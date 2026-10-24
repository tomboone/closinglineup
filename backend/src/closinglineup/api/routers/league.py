"""`GET /api/league`: the league morning page (spec §8). Its handler is a later ticket's; the route
is registered now so its response model is in the OpenAPI schema the frontend's types are generated
from (NEU-1880)."""

from fastapi import APIRouter, HTTPException, status

from closinglineup.api.deps import DateDep
from closinglineup.api.schemas import LeaguePage

router = APIRouter()


@router.get("/api/league")
def league(day: DateDep) -> LeaguePage:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Not implemented yet")
