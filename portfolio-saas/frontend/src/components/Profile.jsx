import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  auth,
  changePassword,
  deleteMe,
  downloadExport,
  logoutSession,
  requestPasswordReset,
  resendVerification,
  updateProfile,
} from "../api.js";

export default function Profile({ user, setUser }) {
  const navigate = useNavigate();
  const [firstName, setFirstName] = useState(user?.first_name || "");
  const [lastName, setLastName] = useState(user?.last_name || "");
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileMsg, setProfileMsg] = useState(null);

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showOldPw, setShowOldPw] = useState(false);
  const [showNewPw, setShowNewPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [savingPassword, setSavingPassword] = useState(false);
  const [passwordMsg, setPasswordMsg] = useState(null);
  const [accountMsg, setAccountMsg] = useState(null);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");


  const handleSaveProfile = async (e) => {
    e.preventDefault();
    setSavingProfile(true);
    setProfileMsg(null);
    try {
      const updated = await updateProfile({
        first_name: firstName.trim(),
        last_name: lastName.trim(),
      });
      setUser((prev) => ({ ...prev, ...updated }));
      setProfileMsg({ type: "success", text: "Profile information updated successfully." });
    } catch (err) {
      setProfileMsg({ type: "error", text: err.message || "Failed to update profile." });
    } finally {
      setSavingProfile(false);
    }
  };

  const handleChangePassword = async (e) => {
    e.preventDefault();
    if (!oldPassword) {
      setPasswordMsg({ type: "error", text: "Please enter your current password." });
      return;
    }
    if (newPassword.length < 8) {
      setPasswordMsg({ type: "error", text: "New password must be at least 8 characters long." });
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordMsg({ type: "error", text: "New passwords do not match." });
      return;
    }

    setSavingPassword(true);
    setPasswordMsg(null);
    try {
      const res = await changePassword(oldPassword, newPassword, confirmPassword);
      auth.tokens = res;
      setPasswordMsg({ type: "success", text: res.detail || "Password changed successfully!" });
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setPasswordMsg({ type: "error", text: err.message || "Failed to change password." });
    } finally {
      setSavingPassword(false);
    }
  };

  const isPasswordValid = newPassword.length >= 8 && !/^\d+$/.test(newPassword);

  const runAccountAction = async (action, success) => {
    setAccountMsg(null);
    try {
      await action();
      setAccountMsg({ type: "success", text: success });
    } catch (error) {
      setAccountMsg({ type: "error", text: error.message });
    }
  };

  const handleDelete = async (event) => {
    event.preventDefault();
    await runAccountAction(
      async () => {
        await deleteMe(deletePassword, deleteConfirmation);
        auth.logout();
        setUser(null);
        navigate("/login");
      },
      "Account deleted.",
    );
  };

  return (
    <div className="profile-container">
      <div className="page-header">
        <h2>Account & Security Settings</h2>
        <p className="subtitle">
          Manage your personal account details, view subscription status, and update your security settings.
        </p>
      </div>

      <div className="profile-grid">
        {/* Card 1: Account Overview & Details */}
        <div className="card profile-card">
          <div className="card-header">
            <h3>Personal Information</h3>
            <span className="card-badge">Account Details</span>
          </div>

          <div className="user-overview-box">
            <div className="avatar-placeholder">
              {(firstName[0] || user?.email[0] || "U").toUpperCase()}
            </div>
            <div className="overview-details">
              <div className="user-name-display">
                {user?.first_name || user?.last_name
                  ? `${user?.first_name || ""} ${user?.last_name || ""}`.trim()
                  : "Account User"}
              </div>
              <div className="user-email-display">{user?.email}</div>
              <div className="badges-row">
                <span className={`tier-badge ${user?.is_pro ? "pro" : "free"}`}>
                  {user?.is_pro ? "PRO Tier" : "FREE Tier"}
                </span>
                {user?.is_staff && <span className="staff-badge">Staff / Admin</span>}
                <span className={`card-badge ${user?.email_verified_at ? "" : "danger"}`}>
                  {user?.email_verified_at ? "Email verified" : "Verification pending"}
                </span>
              </div>
            </div>
          </div>

          {profileMsg && (
            <div
              role="status"
              className={`alert ${profileMsg.type === "success" ? "alert-success" : "alert-error"}`}
            >
              {profileMsg.text}
            </div>
          )}

          <form onSubmit={handleSaveProfile} className="profile-form">
            <div className="form-group">
              <label htmlFor="profile-email">Email Address</label>
              <input
                id="profile-email"
                type="email"
                value={user?.email || ""}
                disabled
                className="input-disabled"
                title="Email is your unique login identifier and cannot be modified."
              />
              <small className="field-help">Your primary login email address.</small>
            </div>

            <div className="form-row">
              <div className="form-group">
                <label htmlFor="first-name">First Name</label>
                <input
                  id="first-name"
                  type="text"
                  value={firstName}
                  onChange={(e) => setFirstName(e.target.value)}
                  placeholder="e.g. Alex"
                />
              </div>
              <div className="form-group">
                <label htmlFor="last-name">Last Name</label>
                <input
                  id="last-name"
                  type="text"
                  value={lastName}
                  onChange={(e) => setLastName(e.target.value)}
                  placeholder="e.g. Morgan"
                />
              </div>
            </div>

            <div className="form-actions">
              <button type="submit" className="primary" disabled={savingProfile}>
                {savingProfile ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </form>
        </div>

        {/* Card 2: Security & Password Change */}
        <div className="card profile-card">
          <div className="card-header">
            <h3>Password & Security</h3>
            <span className="card-badge">Authentication</span>
          </div>

          <p className="section-desc">
            Ensure your account is using a strong password. You will need your current password to set a new one.
          </p>

          {passwordMsg && (
            <div
              role="status"
              className={`alert ${passwordMsg.type === "success" ? "alert-success" : "alert-error"}`}
            >
              {passwordMsg.text}
            </div>
          )}

          <form onSubmit={handleChangePassword} className="profile-form">
            <div className="form-group">
              <div className="label-with-action">
                <label htmlFor="old-password">Current Password</label>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setShowOldPw(!showOldPw)}
                >
                  {showOldPw ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="old-password"
                type={showOldPw ? "text" : "password"}
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                placeholder="Enter current password"
                required
              />
            </div>

            <div className="form-group">
              <div className="label-with-action">
                <label htmlFor="new-password">New Password</label>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setShowNewPw(!showNewPw)}
                >
                  {showNewPw ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="new-password"
                type={showNewPw ? "text" : "password"}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="At least 8 characters"
                required
              />
              <ul className="password-rules">
                <li className={newPassword.length >= 8 ? "valid" : ""}>
                  {newPassword.length >= 8 ? "✓" : "•"} Minimum 8 characters
                </li>
                <li className={newPassword && !/^\d+$/.test(newPassword) ? "valid" : ""}>
                  {newPassword && !/^\d+$/.test(newPassword) ? "✓" : "•"} Cannot be entirely numeric
                </li>
              </ul>
            </div>

            <div className="form-group">
              <div className="label-with-action">
                <label htmlFor="confirm-password">Confirm New Password</label>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setShowConfirmPw(!showConfirmPw)}
                >
                  {showConfirmPw ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="confirm-password"
                type={showConfirmPw ? "text" : "password"}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="Re-enter new password"
                required
              />
              {confirmPassword && newPassword !== confirmPassword && (

                <small className="field-error">Passwords do not match</small>
              )}
            </div>

            <div className="form-actions">
              <button
                type="submit"
                className="primary"
                disabled={savingPassword || (newPassword.length > 0 && !isPasswordValid)}
              >
                {savingPassword ? "Updating Password…" : "Update Password"}
              </button>
            </div>
          </form>
        </div>

        <div className="card profile-card">
          <div className="card-header">
            <h3>Data Rights & Sessions</h3>
            <span className="card-badge">Privacy</span>
          </div>
          {accountMsg && (
            <div role="status" className={`alert ${accountMsg.type === "success" ? "alert-success" : "alert-error"}`}>
              {accountMsg.text}
            </div>
          )}
          <div className="form-actions trust-actions">
            {!user?.email_verified_at && (
              <button type="button" onClick={() => runAccountAction(
                () => resendVerification(user.email),
                "Verification email sent.",
              )}>Resend verification</button>
            )}
            <button type="button" onClick={() => runAccountAction(
              () => requestPasswordReset(user.email),
              "Password reset email sent.",
            )}>Email password reset link</button>
            <button type="button" onClick={() => runAccountAction(
              downloadExport,
              "Export downloaded.",
            )}>Download data export</button>
            <button type="button" onClick={async () => {
              await logoutSession(true);
              setUser(null);
              navigate("/login");
            }}>Log out all devices</button>
          </div>

          <form onSubmit={handleDelete} className="profile-form danger-zone">
            <h4>Delete account</h4>
            <p>This permanently deletes portfolio data. Payment audit rows are pseudonymized.</p>
            <label htmlFor="delete-password">Confirm password</label>
            <input id="delete-password" type="password" value={deletePassword}
              onChange={(event) => setDeletePassword(event.target.value)} required />
            <label htmlFor="delete-confirmation">Type DELETE</label>
            <input id="delete-confirmation" value={deleteConfirmation}
              onChange={(event) => setDeleteConfirmation(event.target.value)}
              pattern="DELETE" required />
            <button className="danger" type="submit"
              disabled={!deletePassword || deleteConfirmation !== "DELETE"}>
              Permanently delete account
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
