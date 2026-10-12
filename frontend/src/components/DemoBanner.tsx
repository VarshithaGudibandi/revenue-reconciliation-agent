import { useMutation } from "@tanstack/react-query";
import { api, type Upload } from "../api/client";
import { useDemo, useRefreshAfterChange } from "../api/hooks";

/** Only shown when the server runs in demo mode, with generated data. */
export function DemoBanner() {
  const { data } = useDemo();
  const refresh = useRefreshAfterChange();
  const load = useMutation<Upload, Error, void>({
    mutationFn: () => api.loadCorrectedExport(),
    onSuccess: () => void refresh(),
  });
  if (!data?.enabled) return null;

  const outcome = load.data?.cases;
  return (
    <div className="banner" role="note">
      <span>
        <strong>Demo data.</strong> Everything here is generated. To see the loop, report a fix on a
        case, then load the corrected export, as if the source system had been fixed.
      </span>
      <button
        type="button"
        className="btn"
        disabled={data.corrected_files_loaded || load.isPending}
        onClick={() => load.mutate()}
      >
        {data.corrected_files_loaded ? "Corrected export loaded" : load.isPending ? "Loading…" : "Load corrected export"}
      </button>
      {outcome?.applied ? (
        <span role="status" className="secondary">
          {outcome.resolved} resolved, {outcome.updated} updated, {outcome.fix_not_verified} fixes not confirmed.
        </span>
      ) : null}
      {load.error ? (
        <span className="error-text" role="alert">
          {load.error.message}
        </span>
      ) : null}
    </div>
  );
}
