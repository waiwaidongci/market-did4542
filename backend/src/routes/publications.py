from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import PublicationState, Role
from src.controllers.auth import allow, current
from src.domain import publication as rules
from src.models.identity import User
from src.models.jobs import Publication
from src.models.source import Dataset
from src.schemas.requests import PublicationRequest, WithdrawalRequest
from src.services import publication as service
from src.services.audit import record
from src.utils import views

router = APIRouter(prefix='/publications')


def handle(function, *args):
    try:
        return function(*args)
    except (service.PublicationConflict, service.PublicationUnavailable) as error:
        raise HTTPException(409, str(error)) from None
    except service.PublicationWriteFailed as error:
        raise HTTPException(503, str(error)) from None


@router.get('')
def ledger(db: Session = Depends(connection), actor: User = Depends(current)):
    """内部复盘台账：全部申请与状态，照旧可查。"""
    return [views.publication(item) for item in
            db.scalars(select(Publication).order_by(Publication.id.desc()).limit(200))]


@router.get('/public')
def public_index(db: Session = Depends(connection), actor: User = Depends(current)):
    """公开区：仅获准的合计，只含合计本身。"""
    return [views.public_totals(item) for item in service.public_totals(db)]


@router.get('/public.csv')
def public_file(db: Session = Depends(connection), actor: User = Depends(current)):
    """公开下载文件：发布时生成，只含获准的合计。"""
    path = service.public_path()
    if not path.is_file():
        try:
            service.write_public(db)
        except OSError:
            raise HTTPException(410, '公开文件暂不可用，请稍后重试') from None
    filename = quote('等候合计_公开.csv')
    return FileResponse(path, media_type='text/csv; charset=utf-8', filename=filename)


@router.post('', status_code=201)
def submit(body: PublicationRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    markets = rules.normalize_markets(body.markets)
    day = rules.parse_day(body.day)
    try:
        item, created, decided = service.submit(db, body.dataset_id, day, markets, actor)
    except service.PublicationConflict as error:
        record(db, actor.username, 'publication.rejected', error.item.id,
               markets=markets, day=day.isoformat(), reason=str(error))
        db.commit()
        raise HTTPException(409, str(error)) from None
    except service.PublicationUnavailable as error:
        record(db, actor.username, 'publication.unavailable', error.item.id,
               markets=markets, day=day.isoformat())
        db.commit()
        raise HTTPException(409, str(error)) from None
    except service.PublicationWriteFailed as error:
        record(db, actor.username, 'publication.failed', error.item.id,
               markets=markets, day=day.isoformat(), reason=str(error))
        db.commit()
        raise HTTPException(503, str(error)) from None
    if decided:
        record(db, actor.username, 'publication.published', item.id,
               markets=markets, day=day.isoformat(), retry=not created)
        db.commit()
    return {**views.publication(item), 'created': created}


@router.post('/{publication_id}/withdraw')
def withdraw(publication_id: int, body: WithdrawalRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN))):
    if not db.get(Publication, publication_id):
        raise HTTPException(404, '找不到该发布申请')
    item = handle(service.withdraw, db, publication_id, actor, body.note)
    record(db, actor.username, 'publication.withdrawn', item.id, note=body.note)
    db.commit()
    return views.publication(item)


@router.get('/{publication_id}/export')
def export(publication_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(Publication, publication_id)
    if not item:
        raise HTTPException(404, '找不到该发布申请')
    if item.state != PublicationState.APPROVED:
        raise HTTPException(409, '只有获准的合计可以下载')
    filename = quote(f'等候合计_{item.day.isoformat()}.csv')
    buffer = service.public_csv(db, [item])
    return Response(buffer, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})
