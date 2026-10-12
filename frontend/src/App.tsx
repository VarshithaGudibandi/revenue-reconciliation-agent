import { useState } from "react";
import { usePeriods, useProfiles } from "./api/hooks";
import { Alerts } from "./components/Alerts";
import { Dashboard } from "./components/Dashboard";
import { DemoBanner } from "./components/DemoBanner";
import { Setup } from "./components/Setup";
import { UploadDialog } from "./components/Dialogs";
import { useSearchParam, useTheme, type Theme } from "./lib/browser";
import { formatPeriod } from "./lib/formatters";

export function App() {
  const profiles = useProfiles();
  const [profileParam, setProfileParam] = useSearchParam("profile");
  const [periodParam, setPeriodParam] = useSearchParam("period");
  const [theme, setTheme] = useTheme();
  const [uploading, setUploading] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const [alerting, setAlerting] = useState(false);

  const profileId =
    profiles.data?.find((p) => p.id === profileParam)?.id ?? profiles.data?.[0]?.id;
  const periods = usePeriods(profileId);
  const period = periods.data?.find((p) => p.period === periodParam)?.period ?? periods.data?.[0]?.period;

  return (
    <>
      <header className="app-header">
        <h1 className="brand">Revenue reconciliation</h1>
        {profiles.data && profiles.data.length > 1 ? (
          <label className="field">
            <span className="sr-only">Profile</span>
            <select className="select" value={profileId} onChange={(e) => { setProfileParam(e.target.value); setPeriodParam(null); }}>
              {profiles.data.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span className="secondary">{profiles.data?.[0]?.name}</span>
        )}
        {periods.data && periods.data.length > 0 ? (
          <label className="field">
            <span className="sr-only">Period</span>
            <select className="select" value={period} onChange={(e) => setPeriodParam(e.target.value)}>
              {periods.data.map((p) => (
                <option key={p.period} value={p.period}>
                  {formatPeriod(p.period)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <span className="header-spacer" />
        {profileId && period ? (
          <button type="button" className="btn" onClick={() => setUploading(true)}>
            Add a file
          </button>
        ) : null}
        {profiles.data && profiles.data.length > 0 && !settingUp ? (
          <button type="button" className="btn" onClick={() => setSettingUp(true)}>
            New reconciliation
          </button>
        ) : null}
        <button type="button" className="btn" onClick={() => setAlerting((on) => !on)} aria-pressed={alerting}>
          Alerts
        </button>
        <label className="field">
          <span className="sr-only">Theme</span>
          <select className="select" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
            <option value="system">System theme</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
      </header>

      <main className="page">
        <DemoBanner profileId={profileId} />
        {profiles.error ? (
          <p className="error-text" role="alert">
            {profiles.error.message}
          </p>
        ) : null}
        {alerting ? <Alerts onClose={() => setAlerting(false)} /> : null}
        {settingUp || (profiles.data && profiles.data.length === 0) ? (
          <Setup
            firstTime={!settingUp}
            onCancel={() => setSettingUp(false)}
            onDone={(result) => {
              setProfileParam(result.profile.id);
              setPeriodParam(result.period);
              setSettingUp(false);
            }}
          />
        ) : null}
        {!settingUp && !alerting && profileId && periods.data && periods.data.length === 0 ? (
          <section className="card">
            <p>No files have been added yet. Use “Add a file” to upload an export.</p>
          </section>
        ) : null}
        {!settingUp && !alerting && profileId && period ? <Dashboard key={`${profileId}-${period}`} profileId={profileId} period={period} /> : null}
      </main>

      {uploading && profileId && period ? (
        <UploadDialog profileId={profileId} period={period} onClose={() => setUploading(false)} />
      ) : null}
    </>
  );
}
