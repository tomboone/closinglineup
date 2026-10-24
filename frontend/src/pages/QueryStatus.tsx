import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";

/** A page's loading and error states; `children` renders its data once it has arrived. */
function QueryStatus<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <p role="status">Loading…</p>;
  if (query.isError) return <p role="alert">Couldn't load this page: {query.error.message}</p>;
  return children(query.data);
}

export default QueryStatus;
