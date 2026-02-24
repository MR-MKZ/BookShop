from fastapi import APIRouter, Depends
from app.auth import get_current_admin_user
from app.models import User

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/")
async def admin_dashboard(current_user: User = Depends(get_current_admin_user)):
    return {"message": f"Welcome Admin {current_user.username}"}
