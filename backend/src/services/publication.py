"""发布申请的处理规则与公开输出。

- 每条申请只提交「某天 + 若干市场」，合计值由同一批门岗底稿（相同校验指纹）现算。
- 同一底稿同一天，既有公开合计（含已撤下的）加上新的一条若能解出任一市场的
  当日数值，就拒绝发布。
- 申请以「底稿校验指纹 + 日期 + 市场集合」的指纹去重：发布失败后重试沿用原
  记录，不重复登记。
- 公开区与下载文件只列出获准的合计；内部台账保留全部申请与状态。
"""

import csv
import io
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from src.config.settings import ARTIFACTS
from src.constants.enums import PublicationState
from src.domain import publication as rules
from src.models.jobs import Publication
from src.models.source import Dataset
from src.utils.exports import safe_cell
from src.utils.time import iso, now

# 曾经公开过的合计（含已撤下的）都参与互补披露核查。
CONSIDERED = (PublicationState.APPROVED, PublicationState.WITHDRAWN)
PUBLIC_COLUMNS = ("观测日期", "所选市场", "市场数", "平均等候合计（分钟）", "获准时间")

# 同一底稿的判重、核查和公开文件替换需要串行。数据库行锁负责跨进程互斥，
# 进程内再加一道锁，保证单实例下并发请求也按顺序读取已提交的合计。
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def serialized(checksum):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(checksum, threading.Lock())
    with lock:
        yield


class PublicationConflict(Exception):
    """新合计与既有合计合用可反推出单个市场数值。"""

    def __init__(self, detail, item=None):
        super().__init__(detail)
        self.item = item


class PublicationUnavailable(Exception):
    """申请已撤下或当前状态不允许该操作。"""

    def __init__(self, detail, item=None):
        super().__init__(detail)
        self.item = item


class PublicationWriteFailed(Exception):
    """公开文件写入失败。"""

    def __init__(self, detail, item=None):
        super().__init__(detail)
        self.item = item


def public_path() -> Path:
    return ARTIFACTS / 'public' / 'wait-totals.csv'


def peers_of(db, dataset):
    """同一校验指纹的底稿批次，互为同一批门岗底稿。"""
    return db.scalars(select(Dataset.id).where(Dataset.checksum == dataset.checksum)).all()


def lock_peers(db, checksum):
    """按固定顺序锁定同一批底稿，串行化同一天的发布申请。"""
    return db.scalars(
        select(Dataset).where(Dataset.checksum == checksum)
        .order_by(Dataset.id).with_for_update()
    ).all()


def published_summaries(db, dataset, day, exclude_id=None):
    """同一天、同一批底稿上已公开的合计（含已撤下的），加锁读取得最新值。"""
    query = select(Publication).where(
        Publication.dataset_id.in_(peers_of(db, dataset)),
        Publication.day == day,
        Publication.state.in_(CONSIDERED),
    ).order_by(Publication.id)
    if exclude_id is not None:
        query = query.where(Publication.id != exclude_id)
    return db.scalars(query.with_for_update()).all()


def disclosed_market(db, dataset, day, markets, exclude_id=None):
    """这次合计与既有合计合用会暴露的市场；没有则 None。"""
    prior = [item.markets for item in published_summaries(db, dataset, day, exclude_id)]
    return rules.conflict(prior, set(markets))


def rejection_detail(market, day):
    return (f'这条合计与已公开（含已撤下）的合计合用，可反推出市场「{market}」'
            f'在 {day.isoformat()} 的数值，拒绝发布。')


def public_totals(db, current=False):
    """公开区只取获准的合计。

    `current=True` 时用加锁的当前读，确保同批底稿串行处理期间读到其他事务刚
    提交的合计，而不是本事务开始时的旧快照。
    """
    query = (select(Publication).where(Publication.state == PublicationState.APPROVED)
             .order_by(Publication.day, Publication.id))
    if current:
        query = query.with_for_update()
    return db.scalars(query).all()


def _row(item):
    return [item.day.isoformat(), '、'.join(item.markets), len(item.markets),
            f'{float(item.total):.2f}', iso(item.decided_at) or '']


def public_csv(db, items=None):
    """公开下载文件只包含获准的合计。"""
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow([safe_cell(column) for column in PUBLIC_COLUMNS])
    writer.writerows([[safe_cell(cell) for cell in _row(item)]
                      for item in (public_totals(db, current=True) if items is None else items)])
    return '﻿' + buffer.getvalue()


def _stage(text):
    """先把公开文件写到唯一临时文件，提交数据库后再原子替换，避免半成品。"""
    path = public_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f'{path.name}.{secrets.token_hex(6)}.tmp')
    staging.write_text(text, encoding='utf-8')
    return staging


def _promote(staging):
    staging.replace(public_path())


def _discard(staging):
    try:
        staging.unlink(missing_ok=True)
    except OSError:
        pass


def write_public(db):
    """按当前获准的合计重建公开文件（首次下载或修复时使用）。"""
    _promote(_stage(public_csv(db)))


def submit(db, dataset_id, day, markets, actor):
    """登记并处理一次发布申请，返回 (记录, 是否新建, 是否本次转为获准)。

    通过核查时先写公开文件再提交；写失败登记为发布失败，重试沿用原记录。
    """
    first = db.get(Dataset, dataset_id)
    if first is None:
        raise KeyError(dataset_id)
    with serialized(first.checksum):
        # 结束进入锁之前开启的读事务，确保读到其他申请刚提交的合计。
        db.rollback()
        dataset = db.get(Dataset, dataset_id)
        return _submit(db, dataset, day, markets, actor)


def _submit(db, dataset, day, markets, actor):
    signature = rules.fingerprint(dataset.checksum, day, markets)
    total = rules.aggregate(dataset.rows, day, markets)
    lock_peers(db, dataset.checksum)
    existing = db.scalar(
        select(Publication).where(Publication.fingerprint == signature).with_for_update()
    )
    created = existing is None
    if existing is not None and existing.state == PublicationState.APPROVED:
        return existing, False, False
    if existing is not None and existing.state == PublicationState.WITHDRAWN:
        raise PublicationUnavailable('这条合计已撤下，不能再发布。', existing)

    culprit = disclosed_market(db, dataset, day, markets,
                               exclude_id=existing.id if existing else None)
    if culprit:
        detail = rejection_detail(culprit, day)
        if existing is None:
            existing = Publication(fingerprint=signature, dataset_id=dataset.id, day=day,
                                   markets=list(markets), applicant=actor.username)
            db.add(existing)
        existing.dataset_id = dataset.id
        existing.day = day
        existing.markets = list(markets)
        existing.total = total
        existing.state = PublicationState.REJECTED
        existing.detail = detail
        existing.reviewer = None
        existing.decided_at = now()
        db.commit()
        raise PublicationConflict(detail, existing)

    if existing is None:
        existing = Publication(fingerprint=signature, dataset_id=dataset.id, day=day,
                               markets=list(markets), applicant=actor.username)
        db.add(existing)
    existing.dataset_id = dataset.id
    existing.day = day
    existing.markets = list(markets)
    existing.total = total
    existing.state = PublicationState.APPROVED
    existing.detail = None
    existing.reviewer = actor.username
    existing.decided_at = now()
    db.flush()
    try:
        # 先写临时文件（失败即登记为发布失败），提交后再原子替换公开文件。
        staging = _stage(public_csv(db))
    except OSError as error:
        return _failed(db, existing, error)
    try:
        db.commit()
    except Exception:
        db.rollback()
        _discard(staging)
        _refresh(db)
        raise
    try:
        _promote(staging)
    except OSError:
        # 文件替换失败但数据库已提交，按已提交数据重建，避免公开文件缺失。
        _refresh(db)
    return existing, created, True


def _failed(db, item, error):
    item.state = PublicationState.FAILED
    item.detail = f'公开文件写入失败：{error}'[:500]
    db.commit()
    raise PublicationWriteFailed('公开文件写入失败，请重试同一条申请。', item) from None


def _refresh(db):
    """回滚后用已提交的数据重建公开文件，失败时忽略。"""
    try:
        _promote(_stage(public_csv(db)))
    except OSError:
        pass


def withdraw(db, publication_id, actor, note):
    """撤下一条已获准的合计；记录保留并继续参与核查。"""
    pending = db.get(Publication, publication_id)
    if pending is None:
        raise KeyError(publication_id)
    dataset = db.get(Dataset, pending.dataset_id)
    with serialized(dataset.checksum):
        db.rollback()
        return _withdraw(db, publication_id, actor, note)


def _withdraw(db, publication_id, actor, note):
    pending = db.get(Publication, publication_id)
    if pending is None:
        raise KeyError(publication_id)
    lock_peers(db, db.get(Dataset, pending.dataset_id).checksum)
    item = db.get(Publication, publication_id, with_for_update=True)
    if item.state == PublicationState.WITHDRAWN:
        return item
    if item.state != PublicationState.APPROVED:
        raise PublicationUnavailable('只有已获准的合计可以撤下。')
    item.state = PublicationState.WITHDRAWN
    item.detail = note
    item.reviewer = actor.username
    item.decided_at = now()
    db.flush()
    try:
        staging = _stage(public_csv(db))
    except OSError:
        db.rollback()
        raise PublicationWriteFailed('公开文件更新失败，撤下未生效，请重试。') from None
    try:
        db.commit()
    except Exception:
        db.rollback()
        _discard(staging)
        _refresh(db)
        raise
    try:
        _promote(staging)
    except OSError:
        _refresh(db)
    return item
