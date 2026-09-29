import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class IetfLocalSource(DefaultFieldsMixin, Base):
    """Ownership + sharing record for one locally-tracked IETF draft (see
    IetfImportService.register_local_source). Deliberately separate from
    Dify's Dataset table: a local source is raw downloaded text on shared
    disk, tracked before (and often without ever becoming) a real Dify
    dataset, so it has no tenant/permission fields to reuse the way a
    Dataset does.

    `owner_account_id` is whichever account first registered this draft --
    ownership isn't reassignable, matching "you own what you pulled first".
    `shared` starts False: a fresh pull is private to its owner until they
    explicitly share it, mirroring Dify's own Dataset.permission default
    of ONLY_ME.
    """

    __tablename__ = "ietf_local_sources"
    __table_args__ = (sa.UniqueConstraint("draft_name", name="ietf_local_sources_draft_name_key"),)

    draft_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    owner_account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    shared: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
