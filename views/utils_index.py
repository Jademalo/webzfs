"""Utilities landing page route and utility availability context."""

from fastapi import APIRouter, Depends, Request

from auth.dependencies import get_current_user
from config.templates import templates
from core.request_context import is_cockpit_request
from services.shell_settings import can_use_shell
from services.utils import is_linux
from services.zfs_delegation import can_manage_zfs_delegation

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/")
def index(request: Request, username: str = Depends(get_current_user)):
    """Display the utilities page with cards for each utility."""
    return templates.TemplateResponse(
        request,
        name="utils/index.jinja",
        context={
            "cockpit_context": is_cockpit_request(request),
            "shell_allowed": can_use_shell(username),
            "delegation_allowed": can_manage_zfs_delegation(username),
            "zed_available": is_linux(),
        },
    )