"""Kaggle account CRUD routes (settings page for the Kaggle Kernels API automation)."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app import kaggle_accounts, kaggle_packages
from app.deps import locked_conn

router = APIRouter()


@router.get("/api/kaggle/packages")
def list_packages(
    request: Request, search: str = "", book_id: int | None = None,
    status: str = "", model: str = "", page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100), sort: str = "updated_at", direction: str = "desc",
):
    with locked_conn(request) as conn:
        return kaggle_packages.list_packages(conn, search=search, book_id=book_id, status=status,
            model=model, page=page, per_page=per_page, sort=sort, direction=direction)


@router.post("/api/kaggle/packages/{package_id}/{action}")
def package_action(request: Request, package_id: int, action: str):
    if action not in {"check", "resume"}:
        raise HTTPException(404, "Thao tác không tồn tại")
    with locked_conn(request) as conn:
        try:
            job_id = kaggle_packages.enqueue_action(conn, package_id, check_only=action == "check")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"job_id": job_id, "package_id": package_id}


@router.post("/kaggle/accounts")
def create_account(request: Request, label: str = Form(...), username: str = Form(...), api_key: str = Form(...)):
    with locked_conn(request) as conn:
        kaggle_accounts.create_account(conn, label.strip(), username.strip(), api_key.strip())
    return RedirectResponse(url="/drive#kaggle", status_code=303)


@router.post("/kaggle/accounts/{account_id}/edit")
def update_account(
    request: Request, account_id: int, label: str = Form(...), username: str = Form(...),
    api_key: str = Form(""),
):
    with locked_conn(request) as conn:
        if kaggle_accounts.get_account(conn, account_id) is None:
            raise HTTPException(status_code=404, detail="Kaggle account not found")
        kaggle_accounts.update_account(
            conn, account_id, label=label.strip(), username=username.strip(), api_key=api_key.strip(),
        )
    return RedirectResponse(url="/drive#kaggle", status_code=303)


@router.post("/kaggle/accounts/{account_id}/toggle")
def toggle_account(request: Request, account_id: int):
    with locked_conn(request) as conn:
        account = kaggle_accounts.get_account(conn, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Kaggle account not found")
        kaggle_accounts.set_disabled(conn, account_id, account["status"] != "disabled")
    return RedirectResponse(url="/drive#kaggle", status_code=303)


@router.post("/kaggle/accounts/{account_id}/delete")
def delete_account(request: Request, account_id: int):
    with locked_conn(request) as conn:
        if kaggle_accounts.get_account(conn, account_id) is None:
            raise HTTPException(status_code=404, detail="Kaggle account not found")
        if not kaggle_accounts.delete_account(conn, account_id):
            raise HTTPException(
                status_code=400,
                detail="Account đang được một job kaggle_tts sử dụng; đợi job xong rồi xoá.",
            )
    return RedirectResponse(url="/drive#kaggle", status_code=303)
