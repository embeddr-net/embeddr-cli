from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select, desc
from embeddr.db.session import get_session
from embeddr.models.generation import Generation
from embeddr.services.generation_service import GenerationService
from pydantic import BaseModel

router = APIRouter()


class GenerationCreate(BaseModel):
    workflow_id: int
    inputs: Dict[str, Any]


class GenerationResponse(BaseModel):
    id: str
    status: str
    prompt_id: Optional[str]
    inputs: Dict[str, Any]
    outputs: List[Dict[str, Any]]
    error_message: Optional[str]
    created_at: Any


@router.post("", response_model=GenerationResponse)
async def create_generation(
    req: GenerationCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    print(f"[API] Creating generation for workflow {req.workflow_id}")
    service = GenerationService(session)
    try:
        # 1. Create Record
        generation = await service.create_generation(req.workflow_id, req.inputs)
        print(f"[API] Generation created: {generation.id}")

        # 2. Submit to ComfyUI (in background or await? await is safer for immediate feedback on queueing)
        # Let's await the submission to ensure it's valid and queued
        generation = await service.submit_generation(generation.id)
        print(
            f"[API] Generation submitted: {generation.id}, Status: {generation.status}, PromptID: {generation.prompt_id}"
        )

        return generation
    except ValueError as e:
        print(f"[API] ValueError creating generation: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        print(f"[API] Error creating generation: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rescan")
async def rescan_generations(session: Session = Depends(get_session)):
    """
    Rescan generations to link missing outputs from ComfyUI history.
    """
    service = GenerationService(session)
    count = await service.rescan_generations()
    return {"message": "Rescanned generations", "updated": count}


@router.get("", response_model=List[Generation])
def list_generations(
    skip: int = 0, limit: int = 50, session: Session = Depends(get_session)
):
    print(f"[API] Listing generations skip={skip} limit={limit}")
    statement = (
        select(Generation)
        .order_by(desc(Generation.created_at))
        .offset(skip)
        .limit(limit)
    )
    results = session.exec(statement).all()
    print(f"[API] Found {len(results)} generations")
    return results


@router.get("/{generation_id}", response_model=Generation)
def get_generation(generation_id: str, session: Session = Depends(get_session)):
    generation = session.get(Generation, generation_id)
    if not generation:
        raise HTTPException(status_code=404, detail="Generation not found")
    return generation
