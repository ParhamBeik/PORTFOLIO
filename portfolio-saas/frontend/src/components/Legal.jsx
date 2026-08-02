import { Link } from "react-router-dom";


export default function Legal({ kind }) {
  const privacy = kind === "privacy";
  return (
    <main className="legal-page">
      <h1>{privacy ? "Privacy Notice" : "Terms of Use"}</h1>
      <p className="alert alert-warning">
        Draft for closed beta. Iranian counsel approval is pending; paid public
        launch remains blocked until that review is recorded.
      </p>
      {privacy ? (
        <>
          <h2>Data We Use</h2>
          <p>We use account, portfolio, ledger, import, payment, and diagnostic data to operate Lattice.</p>
          <h2>Your Rights</h2>
          <p>You can export your account data, revoke sessions, or delete your account from Profile.</p>
          <h2>Retention</h2>
          <p>Account-owned data is deleted with the account. Payment audit records are detached and pseudonymized.</p>
        </>
      ) : (
        <>
          <h2>Informational Use</h2>
          <p>Lattice provides informational analytics, not investment, tax, or legal advice.</p>
          <h2>Data Quality</h2>
          <p>Figures may be delayed, incomplete, estimated, or excluded; trust labels and provenance remain part of the result.</p>
          <h2>Closed Beta</h2>
          <p>Access requires an invitation and may change while the service is evaluated.</p>
        </>
      )}
      <Link to="/login">Back to sign in</Link>
    </main>
  );
}
