from .analyze        import router as analyze_router
from .user_evaluation import router as user_eval_router
from .lessons        import router as lessons_router
from .quiz           import router as quiz_router
from .auth           import router as auth_router
from .admin          import router as admin_router

__all__ = [
    "analyze_router",
    "user_eval_router",
    "lessons_router",
    "quiz_router",
    "auth_router",
    "admin_router",
]
