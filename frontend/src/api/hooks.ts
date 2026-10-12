import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { newIdempotencyKey } from "../lib/formatters";
import { api, type CaseQuery, type CaseDetail } from "./client";

export const useProfiles = () => useQuery({ queryKey: ["profiles"], queryFn: api.profiles });

export const usePeriods = (profileId: string | undefined) =>
  useQuery({
    queryKey: ["periods", profileId],
    queryFn: () => api.periods(profileId!),
    enabled: !!profileId,
  });

export const useSummary = (profileId: string | undefined, period: string | undefined) =>
  useQuery({
    queryKey: ["summary", profileId, period],
    queryFn: () => api.summary(profileId!, period!),
    enabled: !!profileId && !!period,
    placeholderData: keepPreviousData,
  });

export const useCases = (profileId: string | undefined, period: string | undefined, params: CaseQuery) =>
  useQuery({
    queryKey: ["cases", profileId, period, params],
    queryFn: () => api.cases(profileId!, period!, params),
    enabled: !!profileId && !!period,
    placeholderData: keepPreviousData,
  });

export const useMatches = (
  profileId: string | undefined,
  period: string | undefined,
  params: { q?: string; limit?: number; offset?: number },
  enabled: boolean,
) =>
  useQuery({
    queryKey: ["matches", profileId, period, params],
    queryFn: () => api.matches(profileId!, period!, params),
    enabled: enabled && !!profileId && !!period,
    placeholderData: keepPreviousData,
  });

export const useCase = (caseId: string | null) =>
  useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.caseDetail(caseId!),
    enabled: !!caseId,
  });

export const useDemo = () =>
  useQuery({ queryKey: ["demo"], queryFn: api.demo, retry: false, staleTime: Infinity });

/** After anything that changes cases or files, everything derived from them is stale. */
export function useRefreshAfterChange() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      ["summary", "cases", "matches", "case", "periods", "demo"].map((key) =>
        client.invalidateQueries({ queryKey: [key] }),
      ),
    );
}

export function useCaseAction(caseId: string) {
  const client = useQueryClient();
  const refresh = useRefreshAfterChange();
  return useMutation<CaseDetail, Error, (key: string) => Promise<CaseDetail>>({
    mutationFn: (run) => run(newIdempotencyKey()),
    onSuccess: (detail) => {
      client.setQueryData(["case", caseId], detail);
      void refresh();
    },
    onError: () => {
      // A stale version or a state change elsewhere: show the case as it is now.
      void client.invalidateQueries({ queryKey: ["case", caseId] });
    },
  });
}
