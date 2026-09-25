// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";

import {MockERC20} from "../src/MockERC20.sol";
import {NegotiationExchange} from "../src/NegotiationExchange.sol";

/// @title Deploy the exchange and its two mock tokens, and write the deployment manifest
/// @notice One invocation produces one deployment and one manifest file. The manifest is the
///         only thing downstream components are allowed to trust about a deployment's identity:
///         the setup validator compares a live chain against it, the indexer reads its address
///         and start block, the reconstruction tool needs nothing else but an RPC URL, and
///         acceptance A17 is the check that the three agree.
/// @dev docs/architecture.md section 3.4, docs/data_model.md section 3.2, ADR-038.
///
///      Usage:
///
///          DEPLOYMENT_ID=local-2026-09-24-01 \
///          OPERATOR_ADDRESS=0x… RELAY_ADDRESS=0x… \
///          forge script script/Deploy.s.sol:Deploy \
///              --rpc-url "$ANVIL_RPC_URL" --broadcast --private-key "$DEPLOYER_PRIVATE_KEY"
///
///      Run from inside `contracts/`. From the repository root the script path becomes
///      `contracts/script/Deploy.s.sol:Deploy` and `--root contracts` is required: the path
///      resolves against the working directory, `fs_permissions` and the `out/` read against
///      `--root`.
///
///      Two things this script deliberately does not do. It does not mint or approve: funding
///      belongs to a run, not to a deployment, and a deployment that pre-funded wallets would
///      make "fresh test wallets per run" (docs/protocol.md section 1) untrue. And it does not
///      resolve ENS: names are display metadata added to the manifest once, by hand, at
///      registration time ([ADR-030](../../docs/decision_log.md)).
contract Deploy is Script {
    /// @dev docs/protocol.md section 15. Bumped only with the document and the domain version.
    string internal constant PROTOCOL_VERSION = "1";
    /// @dev The shape of this file. Independent of the protocol version: a new optional manifest
    ///      field is not a new protocol.
    string internal constant MANIFEST_VERSION = "1";

    /// @dev docs/protocol.md section 2. Both tokens, always.
    uint8 internal constant TOKEN_DECIMALS = 6;

    function run() external {
        string memory deploymentId = vm.envString("DEPLOYMENT_ID");
        address operator = vm.envAddress("OPERATOR_ADDRESS");
        address relay = vm.envAddress("RELAY_ADDRESS");
        string memory explorerBaseUrl = vm.envOr("EXPLORER_BASE_URL", string(""));
        string memory manifestDir = vm.envOr("MANIFEST_DIR", string("../docs/deployments"));

        // Everything that can be refused is refused before a single transaction is broadcast.
        // The alternative is a deployment that exists on chain and cannot be recorded, or that
        // overwrites the record of an earlier one — and on Sepolia that record is the committed
        // evidence a reader is asked to check.
        string memory path = string.concat(manifestDir, "/", deploymentId, ".json");
        _requireValidDeploymentId(deploymentId);
        _requireManifestIsWritable(path);

        vm.startBroadcast();
        MockERC20 baseToken = new MockERC20("Mock Asset", "mASSET", operator);
        MockERC20 quoteToken = new MockERC20("Mock USD", "mUSD", operator);
        NegotiationExchange exchange =
            new NegotiationExchange(address(baseToken), address(quoteToken), operator);
        vm.stopBroadcast();

        _writeManifest(
            path, deploymentId, explorerBaseUrl, exchange, baseToken, quoteToken, operator, relay
        );

        console.log("exchange   ", address(exchange));
        console.log("base token ", address(baseToken));
        console.log("quote token", address(quoteToken));
        console.log("manifest   ", path);
        console.log("");
        console.log("The manifest is written during simulation, before Foundry broadcasts.");
        console.log("If the broadcast below fails, DELETE the manifest: it would describe a");
        console.log("deployment that never landed.");
    }

    // ---------------------------------------------------------------------------------
    // Preconditions
    // ---------------------------------------------------------------------------------

    /// @dev The manifest has to satisfy `deployment_manifest.v1.json`, whose `deployment_id`
    ///      pattern is `^[a-z0-9]+(-[a-z0-9]+)*$`. Checked here rather than left to the schema,
    ///      because the schema is checked after the file is written and the gas is already spent.
    function _requireValidDeploymentId(string memory deploymentId) private pure {
        bytes memory raw = bytes(deploymentId);
        require(raw.length > 0, "DEPLOYMENT_ID is empty");
        require(raw.length <= 64, "DEPLOYMENT_ID is longer than 64 characters");

        bool previousWasHyphen = true; // leading hyphen is as invalid as a doubled one
        for (uint256 i = 0; i < raw.length; i++) {
            bytes1 character = raw[i];
            bool isLower = character >= 0x61 && character <= 0x7A; // a-z
            bool isDigit = character >= 0x30 && character <= 0x39; // 0-9
            bool isHyphen = character == 0x2D; // -

            require(
                isLower || isDigit || isHyphen,
                "DEPLOYMENT_ID must match ^[a-z0-9]+(-[a-z0-9]+)*$ (lowercase, digits, hyphens)"
            );
            require(!(isHyphen && previousWasHyphen), "DEPLOYMENT_ID has a leading or doubled '-'");
            previousWasHyphen = isHyphen;
        }
        require(!previousWasHyphen, "DEPLOYMENT_ID ends with '-'");
    }

    /// @dev A manifest is the identity of one deployment, so overwriting one destroys the only
    ///      record of the deployment it described. Set `MANIFEST_OVERWRITE=true` to replace one
    ///      deliberately — which is what a redeploy under a reused local id wants.
    function _requireManifestIsWritable(string memory path) private view {
        if (!vm.exists(path)) return;
        require(
            vm.envOr("MANIFEST_OVERWRITE", false),
            string.concat(
                "a manifest already exists at ",
                path,
                " -- use a new DEPLOYMENT_ID, or set MANIFEST_OVERWRITE=true to replace it"
            )
        );
    }

    // ---------------------------------------------------------------------------------
    // Manifest
    // ---------------------------------------------------------------------------------

    /// @dev Schema: `packages/protocol/schemas/deployment_manifest.v1.json`.
    ///
    ///      `deployed_at_ts` is the *block* timestamp, not the deployer's wall clock, and the
    ///      manifest records it as integer Unix seconds rather than as a formatted date. Chain
    ///      time is authoritative everywhere else in this system (docs/protocol.md section 6);
    ///      a manifest whose timestamp came from the machine that ran the script would be the
    ///      one place it was not, and the difference would be invisible. The backend renders the
    ///      `TIMESTAMPTZ` column from this value (docs/data_model.md section 3.2).
    ///
    ///      `start_block` is named for what it is rather than for what would be convenient. A
    ///      `forge script` runs its simulation against the current head and broadcasts
    ///      afterwards, so `block.number` here is the head *before* the deployment transactions
    ///      land — at or immediately before the block that holds them. That is exactly the
    ///      guarantee a log scan needs, and it is the only one available from inside the script.
    ///      Calling the field `deployed_at_block` would have claimed a precision it does not
    ///      have; on a fresh Anvil it reads 0 while all three contracts land in the next block.
    function _writeManifest(
        string memory path,
        string memory deploymentId,
        string memory explorerBaseUrl,
        NegotiationExchange exchange,
        MockERC20 baseToken,
        MockERC20 quoteToken,
        address operator,
        address relay
    ) private {
        string memory root = "manifest";

        vm.serializeString(root, "manifest_version", MANIFEST_VERSION);
        vm.serializeString(root, "deployment_id", deploymentId);
        vm.serializeString(root, "protocol_version", PROTOCOL_VERSION);
        vm.serializeUint(root, "chain_id", block.chainid);
        vm.serializeAddress(root, "exchange_address", address(exchange));
        vm.serializeAddress(root, "base_token_address", address(baseToken));
        vm.serializeAddress(root, "quote_token_address", address(quoteToken));
        vm.serializeAddress(root, "operator_address", operator);
        vm.serializeAddress(root, "relay_address", relay);
        vm.serializeUint(root, "token_decimals", TOKEN_DECIMALS);
        vm.serializeUint(root, "start_block", block.number);
        string memory json = vm.serializeUint(root, "deployed_at_ts", block.timestamp);

        vm.writeJson(json, path);

        // The nested objects and the two nullable display fields are written into the file by
        // key, not merged into `root` first. `vm.serializeJson` sets an object's whole contents
        // rather than adding to them, so composing the manifest that way silently produced a
        // one-key file — which the deployment integration test is what caught.
        vm.writeJson(_codeHashes(exchange, baseToken, quoteToken), path, ".code_hashes");
        vm.writeJson(_compiler(), path, ".compiler");

        // Explicit JSON nulls. An absent explorer or an unregistered name is a fact about the
        // deployment, so the manifest states it rather than omitting the key: a consumer that
        // reads a missing key as null cannot tell that apart from a manifest written by an older
        // script (ADR-030, docs/api_contract.md section 2.1).
        vm.writeJson("null", path, ".ens");
        if (bytes(explorerBaseUrl).length == 0) {
            vm.writeJson("null", path, ".explorer_base_url");
        } else {
            vm.writeJson(string.concat('"', explorerBaseUrl, '"'), path, ".explorer_base_url");
        }
    }

    /// @dev keccak256 of the deployed runtime bytecode, which is what the setup validator can
    ///      read back from a live chain. The creation-code hash could not be checked that way.
    function _codeHashes(NegotiationExchange exchange, MockERC20 baseToken, MockERC20 quoteToken)
        private
        returns (string memory)
    {
        string memory key = "code_hashes";
        vm.serializeBytes32(key, "exchange", address(exchange).codehash);
        vm.serializeBytes32(key, "base_token", address(baseToken).codehash);
        return vm.serializeBytes32(key, "quote_token", address(quoteToken).codehash);
    }

    /// @dev Read out of the build artefact rather than restated here, so the manifest cannot
    ///      claim optimizer settings the bytecode was not built with. Changing anything in
    ///      `foundry.toml`'s default profile changes the deployed artefact and belongs in a
    ///      decision-log entry (ADR-033); this is what makes that visible after the fact.
    function _compiler() private returns (string memory) {
        string memory artefact = vm.readFile("out/NegotiationExchange.sol/NegotiationExchange.json");

        string memory key = "compiler";
        vm.serializeString(key, "solc", vm.parseJsonString(artefact, "$.metadata.compiler.version"));
        vm.serializeBool(
            key, "optimizer", vm.parseJsonBool(artefact, "$.metadata.settings.optimizer.enabled")
        );
        vm.serializeUint(
            key, "runs", vm.parseJsonUint(artefact, "$.metadata.settings.optimizer.runs")
        );
        vm.serializeString(
            key,
            "bytecode_hash",
            vm.parseJsonString(artefact, "$.metadata.settings.metadata.bytecodeHash")
        );
        return vm.serializeString(
            key, "evm_version", vm.parseJsonString(artefact, "$.metadata.settings.evmVersion")
        );
    }
}
