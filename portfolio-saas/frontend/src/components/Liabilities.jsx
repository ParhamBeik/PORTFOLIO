/**
 * Debts, and what is actually owed on them today.
 *
 * Lifted out of `Dashboard.jsx` when a liability stopped being a label and a
 * number. A loan is a repayment schedule: the balance is derived from its
 * terms rather than re-typed every month, and the three shapes a debt comes in
 * -- a bank loan, a debt secured on one asset, and everything else -- differ in
 * ways the rest of the app depends on. `Liability.outstanding_tomans` on the
 * server is the matching authority; nothing here computes a balance.
 */
import { useState } from "react";

import {
  createLiability,
  deleteLiability,
  listLiabilities,
  updateLiability,
} from "../api.js";
import { date, holdingLabel, toman } from "../format.js";
import { useApi } from "../useApi.js";
import {
  Async,
  Badge,
  Button,
  Card,
  Empty,
  ErrorState,
  Field,
  Input,
  JalaliDateField,
  Modal,
  Select,
  Tabs,
} from "./ui.jsx";

// What a debt IS, in the words someone would use for it. `secured` decides
// whether the form insists on naming an asset; `terms` whether it offers a
// repayment schedule. Both map onto the server's `kind` unchanged.
const LIABILITY_KINDS = [
  {
    value: "bank_loan",
    label: "Bank loan",
    blurb: "Borrowed from a bank and repaid in installments.",
    terms: true,
  },
  {
    value: "secured_debt",
    label: "Debt on an asset",
    blurb: "A mortgage, or anything else borrowed against something you own.",
    secured: true,
    terms: true,
  },
  {
    value: "other",
    label: "Other",
    blurb: "Anything else you want subtracted from net worth.",
  },
];

// How the balance is arrived at. "Rate" and "installment" are the two ways an
// Iranian loan is actually quoted -- a bank contract states a rate, and the
// borrower usually remembers the installment -- and picking neither leaves the
// old behaviour: one number, typed, never moving.
const BALANCE_MODES = [
  { value: "declared", label: "Just the balance" },
  { value: "rate", label: "Rate and term" },
  { value: "installment", label: "Installments" },
];

const modeFor = (row) => {
  if (!row) return "declared";
  if (row.balance_basis === "amortized") return "rate";
  if (row.balance_basis === "installments") return "installment";
  return "declared";
};

/** Every asset held in the portfolios in scope, as liability-attachment options. */
function securableAssets(accounts, accountId) {
  const scoped = accountId
    ? accounts.filter((a) => a.id === Number(accountId))
    : accounts;
  return scoped.flatMap((account) =>
    (account.holdings || []).map((holding) => ({
      key: holding.asset_key,
      label: holdingLabel(holding),
      accountName: account.name,
    }))
  );
}

export function LiabilityDialog({ accountId, accounts, row, onClose, onSaved }) {
  const [form, setForm] = useState({
    account: String(accountId || row?.account_id || ""),
    label: row?.label || "",
    kind: row?.kind || "bank_loan",
    lender: row?.lender || "",
    assetKey: row?.asset_key || "",
    mode: modeFor(row),
    amount: row?.amount_tomans != null ? String(row.amount_tomans) : "",
    principal: row?.principal_tomans != null ? String(row.principal_tomans) : "",
    rate: row?.annual_rate_pct != null ? String(row.annual_rate_pct) : "",
    termMonths: row?.term_months != null ? String(row.term_months) : "",
    installment:
      row?.monthly_installment_tomans != null
        ? String(row.monthly_installment_tomans)
        : "",
    // The server holds a plain date; `JalaliDateField` speaks ISO instants.
    startedOn: row?.started_on ? `${row.started_on}T00:00:00` : "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (key) => (e) => setForm((current) => ({ ...current, [key]: e.target.value }));
  const put = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const kind = LIABILITY_KINDS.find((k) => k.value === form.kind) || LIABILITY_KINDS[0];
  const assets = securableAssets(accounts, form.account);
  const scheduled = kind.terms && form.mode !== "declared";

  // Said out loud rather than discovered by pressing a disabled button. Every
  // branch mirrors a rule the serializer enforces, so the two cannot disagree
  // about what a complete form is.
  const problem = (() => {
    if (!form.account) return "Choose a portfolio.";
    if (!form.label.trim()) return "Give the debt a name.";
    if (kind.secured && !form.assetKey) return "Choose the asset this debt is secured against.";
    if (!scheduled) {
      return Number(form.amount) > 0 ? "" : "Enter what is still owed.";
    }
    if (!(Number(form.termMonths) > 0)) return "Enter how many monthly installments there are in total.";
    if (!form.startedOn) return "Choose the month the first installment was due.";
    if (form.mode === "rate") {
      if (!(Number(form.principal) > 0)) return "Enter the amount borrowed.";
      if (!(Number(form.rate) >= 0)) return "Enter the annual interest rate.";
      return "";
    }
    if (!(Number(form.installment) > 0)) return "Enter the monthly installment.";
    return "";
  })();

  const payload = {
    label: form.label.trim(),
    kind: form.kind,
    lender: form.lender.trim(),
    assetKey: form.assetKey || null,
    // Cleared explicitly, not omitted. Switching a scheduled loan back to a
    // plain balance has to REMOVE the terms, or `balance_basis` keeps
    // amortizing from figures the form no longer shows.
    amountTomans: scheduled ? null : form.amount,
    principalTomans: scheduled && form.mode === "rate" ? form.principal : null,
    annualRatePct: scheduled && form.mode === "rate" ? form.rate : null,
    termMonths: scheduled ? form.termMonths : null,
    monthlyInstallmentTomans:
      scheduled && form.mode === "installment" ? form.installment : null,
    startedOn: scheduled ? form.startedOn.slice(0, 10) : null,
  };

  const save = async () => {
    if (problem) return;
    setBusy(true);
    setError("");
    try {
      if (row) {
        await updateLiability(row.account_id, row.id, payload);
      } else {
        await createLiability(Number(form.account), payload);
      }
      await onSaved?.();
      onClose();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={row ? "Edit liability" : "Add liability"}
      subtitle="Debts are subtracted from your net worth. A loan on a schedule is worked out from its terms, so the balance keeps itself current."
      onClose={onClose}
      testId="liability-dialog"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="primary"
            onClick={save}
            disabled={busy || !!problem}
            data-testid="liability-save"
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <ErrorState error={new Error(error)} testId="liability-error" />}

        {!accountId && (
          <Field label="Portfolio">
            <Select
              label="Portfolio"
              className="w-full"
              value={form.account}
              onChange={set("account")}
              data-testid="liability-account"
            >
              <option value="">Choose a portfolio</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>{account.name}</option>
              ))}
            </Select>
          </Field>
        )}

        <Field label="What kind of debt is this?" hint={kind.blurb}>
          <Tabs
            options={LIABILITY_KINDS.map(({ value, label }) => ({ value, label }))}
            value={form.kind}
            onChange={(value) => put("kind", value)}
            label="Kind of debt"
            testId="liability-kind"
          />
        </Field>

        <Field label="What is it?">
          <Input
            label="What is it?"
            className="w-full"
            placeholder="Mortgage on the Tehran flat"
            value={form.label}
            onChange={set("label")}
            data-testid="liability-label"
          />
        </Field>

        {kind.terms && (
          <Field label="Who is it owed to?">
            <Input
              label="Lender"
              className="w-full"
              placeholder="Bank Maskan"
              value={form.lender}
              onChange={set("lender")}
              data-testid="liability-lender"
            />
          </Field>
        )}

        {(kind.secured || kind.terms) && (
          <Field
            label={kind.secured ? "Secured against" : "Secured against (optional)"}
            hint="A debt tied to an asset rides with it: hide the asset and its debt goes too, so net worth never drops by the loan alone."
          >
            <Select
              label="Secured against"
              className="w-full"
              value={form.assetKey}
              onChange={set("assetKey")}
              data-testid="liability-asset"
            >
              <option value="">Not secured against anything</option>
              {assets.map((asset) => (
                <option key={`${asset.accountName}-${asset.key}`} value={asset.key}>
                  {asset.label}
                  {!form.account ? ` · ${asset.accountName}` : ""}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {kind.terms ? (
          <Field label="How is the balance worked out?">
            <Tabs
              options={BALANCE_MODES}
              value={form.mode}
              onChange={(value) => put("mode", value)}
              label="Balance mode"
              testId="liability-mode"
            />
          </Field>
        ) : null}

        {!scheduled && (
          <Field label="Amount still owed (Toman)">
            <Input
              label="Amount still owed"
              type="number"
              min="0"
              step="any"
              className="w-full"
              value={form.amount}
              onChange={set("amount")}
              data-testid="liability-amount"
            />
          </Field>
        )}

        {scheduled && (
          <div className="space-y-4 rounded-lg border border-border bg-panel-2 p-3">
            {form.mode === "rate" ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Field label="Amount borrowed (Toman)">
                  <Input
                    label="Amount borrowed"
                    type="number"
                    min="0"
                    step="any"
                    className="w-full"
                    value={form.principal}
                    onChange={set("principal")}
                    data-testid="liability-principal"
                  />
                </Field>
                <Field label="Annual rate (%)">
                  <Input
                    label="Annual rate"
                    type="number"
                    min="0"
                    step="any"
                    className="w-full"
                    value={form.rate}
                    onChange={set("rate")}
                    data-testid="liability-rate"
                  />
                </Field>
              </div>
            ) : (
              <Field label="Monthly installment (Toman)">
                <Input
                  label="Monthly installment"
                  type="number"
                  min="0"
                  step="any"
                  className="w-full"
                  value={form.installment}
                  onChange={set("installment")}
                  data-testid="liability-installment"
                />
              </Field>
            )}
            <Field label="Total number of installments">
              <Input
                label="Total number of installments"
                type="number"
                min="1"
                step="1"
                className="w-full"
                value={form.termMonths}
                onChange={set("termMonths")}
                data-testid="liability-term"
              />
            </Field>
            <Field
              label="First installment was due"
              hint="Installments are counted on the Jalali calendar, which is when they actually fall."
            >
              <JalaliDateField
                value={form.startedOn}
                onChange={(value) => put("startedOn", value)}
                testId="liability-started-on"
                todayLabel="Choose a month"
              />
            </Field>
          </div>
        )}

        {problem && (
          <p className="text-xs text-muted" data-testid="liability-incomplete">{problem}</p>
        )}
      </div>
    </Modal>
  );
}

const LIABILITY_KIND_LABEL = {
  bank_loan: "Bank loan",
  secured_debt: "Secured debt",
  other: "Other",
};

/**
 * One debt, with enough of its terms on screen to recognise it.
 *
 * A row that is a name and a number cannot tell a mortgage from money owed to
 * a cousin, and it cannot say whether the number is still true. Both matter:
 * a secured debt rides with its asset through every figure the app computes,
 * and an amortized balance is derived where a declared one is just the last
 * thing someone typed.
 */
export function LiabilityRow({ row, showAccount, onEdit, onDelete }) {
  const owed = Number(row.outstanding_tomans ?? row.amount_tomans);
  const scheduled = row.balance_basis !== "declared";
  const paidOff =
    scheduled && Number(row.principal_tomans || 0) > 0
      ? 1 - owed / Number(row.principal_tomans)
      : null;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 pb-2 last:border-0">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{row.label}</span>
          <Badge variant={row.kind === "secured_debt" ? "warn" : "neutral"}>
            {LIABILITY_KIND_LABEL[row.kind] || "Other"}
          </Badge>
          {row.asset_label && (
            <Badge variant="neutral" title="This debt rides with the asset it is secured against.">
              on {row.asset_label}
            </Badge>
          )}
        </div>
        <div className="mt-1 text-xs text-muted">
          {[
            showAccount ? row.account_name : null,
            row.lender || null,
            // Only for a scheduled loan. On a declared balance there is no
            // progress to report and printing "0 of 0" invents one.
            scheduled && row.term_months
              ? `${row.installments_paid} of ${row.term_months} installments paid`
              : null,
            scheduled && row.scheduled_installment_tomans
              ? `${toman(row.scheduled_installment_tomans)} / month`
              : null,
            scheduled && row.payoff_on ? `paid off ${date(row.payoff_on)}` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        {paidOff != null && (
          <div className="mt-1.5 h-1 w-40 max-w-full overflow-hidden rounded-full bg-border">
            <div
              className="h-full rounded-full bg-[var(--c-good-fill)]"
              style={{ width: `${Math.max(0, Math.min(1, paidOff)) * 100}%` }}
            />
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        <div className="text-right">
          <div className="text-sm tabular-nums">{toman(owed)}</div>
          {/* Where the number came from. A figure someone typed six months ago
              and one the schedule produced this morning are not the same claim. */}
          <div className="text-[11px] text-muted">
            {scheduled ? "still owed" : "as entered"}
          </div>
        </div>
        <Button variant="ghost" onClick={onEdit} data-testid="liability-edit">Edit</Button>
        <Button variant="danger" onClick={onDelete} data-testid="liability-delete">Delete</Button>
      </div>
    </div>
  );
}

export default function LiabilitiesCard({ activeId, accounts }) {
  const accountKey = accounts.map((a) => a.id).join("|");
  // Every row carries its owning portfolio id in BOTH branches. Edit and delete
  // address a per-account URL, and only the all-portfolios branch used to set
  // the field -- so with a portfolio selected, saving an edit PATCHed
  // /api/accounts/undefined/liabilities/. The serializer's own `account` is the
  // authority; the loop index is the fallback.
  const state = useApi(
    () =>
      activeId
        ? listLiabilities(activeId).then((items) =>
            items.map((item) => ({ ...item, account_id: item.account ?? activeId }))
          )
        : Promise.all(accounts.map((account) => listLiabilities(account.id))).then((rows) =>
            rows.flatMap((items, index) =>
              items.map((item) => ({
                ...item,
                account_id: item.account ?? accounts[index].id,
                account_name: accounts[index].name,
              }))
            )
          ),
    [activeId, accountKey],
    { enabled: accounts.length > 0 },
  );
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");

  const reload = () => state.reload?.();
  const remove = async (row) => {
    if (!window.confirm(`Delete ${row.label}?`)) return;
    setError("");
    try {
      await deleteLiability(row.account_id || activeId, row.id);
      await reload();
    } catch (e) {
      setError(e.message || String(e));
    }
  };

  return (
    <Card
      title="Liabilities"
      subtitle="Debts subtracted from the total above."
      testId="dashboard-liabilities"
      actions={
        <Button variant="ghost" onClick={() => setAdding(true)} data-testid="liability-add">
          Add liability
        </Button>
      }
    >
      {error && <p role="alert" className="mb-2 text-sm text-[var(--c-critical-text)]">{error}</p>}
      <Async {...state} testId="dashboard-liabilities-body" empty="No liabilities recorded.">
        {(rows) => !rows.length ? (
          // `Async` renders `empty` only for a null payload, and an empty list
          // is not null -- without this the card body is simply blank.
          <Empty testId="dashboard-liabilities-empty">No liabilities recorded.</Empty>
        ) : (
          <div className="space-y-2">
            {rows.map((row) => (
              <LiabilityRow
                key={`${row.account_id}-${row.id}`}
                row={row}
                showAccount={!activeId}
                onEdit={() => setEditing(row)}
                onDelete={() => remove(row)}
              />
            ))}
            <div
              className="flex items-center justify-between gap-3 pt-1 text-sm font-medium"
              data-testid="dashboard-liabilities-total"
            >
              <span>Total owed</span>
              <span className="tabular-nums">
                {toman(rows.reduce((sum, r) => sum + Number(r.outstanding_tomans || 0), 0))}
              </span>
            </div>
          </div>
        )}
      </Async>
      {(adding || editing) && (
        <LiabilityDialog
          accountId={activeId}
          accounts={accounts}
          row={editing}
          onClose={() => { setAdding(false); setEditing(null); }}
          onSaved={reload}
        />
      )}
    </Card>
  );
}
