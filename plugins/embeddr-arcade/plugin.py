import logging
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Dict

logger = logging.getLogger(__name__)

# Simple in-memory storage
LEADERBOARDS: Dict[str, List[Dict]] = {
    "tetris": [],
    "invaders": [],
    "snake": []
}


class ScoreSubmission(BaseModel):
    player: str
    score: int


def register(router: APIRouter):
    logger.info("Hello from Embeddr Arcade Python Plugin!")

    @router.get("/highscores/{game}")
    def get_highscores(game: str):
        scores = LEADERBOARDS.get(game, [])
        # Sort by score descending and take top 10
        sorted_scores = sorted(
            scores, key=lambda x: x['score'], reverse=True)[:10]
        return {"scores": sorted_scores}

    @router.post("/highscores/{game}")
    def submit_score(game: str, submission: ScoreSubmission):
        if game not in LEADERBOARDS:
            LEADERBOARDS[game] = []

        entry = submission.dict()
        LEADERBOARDS[game].append(entry)

        # Keep only top 50 in memory
        LEADERBOARDS[game] = sorted(
            LEADERBOARDS[game], key=lambda x: x['score'], reverse=True)[:50]

        return {"status": "success"}
