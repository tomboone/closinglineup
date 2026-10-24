"""`GET /api/games/{game_id}`: a game's page (spec §8). Its handler is a later ticket's; the route
is registered now so its response model is in the OpenAPI schema the frontend's types are
generated from (NEU-1880)."""

from fastapi import APIRouter, HTTPException, status

from closinglineup.api.schemas import GamePage

router = APIRouter()


@router.get("/api/games/{game_id}")
def game_page(game_id: str) -> GamePage:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Not implemented yet")
