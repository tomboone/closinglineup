"""`GET /api/players/{id}`: a player's page (spec §8). Its handler is a later ticket's; the route is
registered now so its response model is in the OpenAPI schema the frontend's types are generated
from (NEU-1880)."""

from fastapi import APIRouter, HTTPException, status

from closinglineup.api.deps import DateDep
from closinglineup.api.schemas import PlayerPage, Window

router = APIRouter()


@router.get("/api/players/{player_id}")
def player_page(player_id: int, day: DateDep, window: Window = "10") -> PlayerPage:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Not implemented yet")
