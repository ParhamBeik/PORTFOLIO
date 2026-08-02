import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import {
  confirmPasswordReset,
  requestPasswordReset,
  verifyEmail,
} from "../api.js";


export function VerifyEmail() {
  const token = new URLSearchParams(useLocation().search).get("token") || "";
  const [message, setMessage] = useState("Verifying email…");

  useEffect(() => {
    verifyEmail(token)
      .then(() => setMessage("Email verified. You can now sign in."))
      .catch((error) => setMessage(error.message));
  }, [token]);

  return <ActionCard title="Email Verification" message={message} />;
}


export function ResetPassword() {
  const params = new URLSearchParams(useLocation().search);
  const uid = params.get("uid") || "";
  const token = params.get("token") || "";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");

  const submit = async (event) => {
    event.preventDefault();
    try {
      if (uid && token) {
        await confirmPasswordReset(uid, token, password, confirm);
        setMessage("Password reset. Sign in with your new password.");
      } else {
        await requestPasswordReset(email);
        setMessage("If the account exists, a reset email was sent.");
      }
    } catch (error) {
      setMessage(error.message);
    }
  };

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <h1>Reset Password</h1>
        {uid && token ? (
          <>
            <label htmlFor="reset-password">New password</label>
            <input id="reset-password" type="password" value={password}
              onChange={(event) => setPassword(event.target.value)} required />
            <label htmlFor="reset-confirm">Confirm password</label>
            <input id="reset-confirm" type="password" value={confirm}
              onChange={(event) => setConfirm(event.target.value)} required />
          </>
        ) : (
          <>
            <label htmlFor="reset-email">Email</label>
            <input id="reset-email" type="email" value={email}
              onChange={(event) => setEmail(event.target.value)} required />
          </>
        )}
        <button className="primary" type="submit">Send</button>
        {message && <p role="status">{message}</p>}
        <Link to="/login">Back to sign in</Link>
      </form>
    </div>
  );
}


function ActionCard({ title, message }) {
  return (
    <div className="auth-wrap">
      <div className="auth-card">
        <h1>{title}</h1>
        <p role="status">{message}</p>
        <Link to="/login">Continue to sign in</Link>
      </div>
    </div>
  );
}
