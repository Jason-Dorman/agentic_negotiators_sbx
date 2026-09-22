/**
 * Placeholder shell. The setup screen, live view, replay mode and export download are
 * stage 4 of docs/build_plan.md.
 *
 * The disclosure below is not decoration: every screen labels test assets and simulated
 * economics (docs/prd.md, docs/security_and_trust_boundaries.md section 9), and it is
 * present from the first commit so it is never added as an afterthought.
 */
export function App() {
  return (
    <main>
      <h1>Agent Negotiation Sandbox</h1>
      <p>
        Test assets on a test network. All balances, prices and mandates are manufactured experiment
        inputs and carry no economic value.
      </p>
      <p>Interface not yet built — see docs/build_plan.md, stage 4.</p>
    </main>
  );
}
