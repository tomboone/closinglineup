import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";
import { routes } from "./routes";

describe("routes", () => {
  it.each([
    ["/", "League"],
    ["/teams/BOS", "Team"],
    ["/players/1628369", "Player"],
    ["/games/0022500001", "Game"],
  ])("%s renders the %s placeholder", (path, name) => {
    const router = createMemoryRouter(routes, { initialEntries: [path] });
    render(<RouterProvider router={router} />);
    expect(screen.getByRole("heading", { name })).toBeInTheDocument();
  });
});
