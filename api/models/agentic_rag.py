import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class PrismUserLlmCredential(DefaultFieldsMixin, Base):
    """One account's own bring-your-own-key credentials for one model
    provider (e.g. "langgenius/openai/openai") -- lets a Prism user's
    Research-tab requests bill to their own API key instead of the shared
    org credential Dify's tenant-wide Provider configuration normally
    supplies. Deliberately account-scoped, not tenant-scoped, since that's
    the whole point: this is NOT the same thing as Dify's own
    Provider/ProviderCredential tables (which are shared org-wide),
    hence a new table rather than reusing those.

    encrypted_config holds a JSON object shaped exactly like the target
    provider's own provider_credential_schema.credential_form_schemas
    (e.g. {"openai_api_key": "...", "openai_organization": "..."} for
    langgenius/openai/openai) -- whichever of those fields are marked
    secret-input are individually encrypted via core.helper.encrypter
    (same mechanism Dify's own provider_credentials table uses), the rest
    stored in the clear, so non-secret fields can be redisplayed to the
    user without a decrypt round-trip.
    """

    __tablename__ = "prism_user_llm_credentials"
    __table_args__ = (
        sa.UniqueConstraint("account_id", "provider", name="prism_user_llm_credential_account_provider_key"),
        sa.Index("prism_user_llm_credential_account_idx", "account_id"),
    )

    account_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    encrypted_config: Mapped[str] = mapped_column(sa.Text, nullable=False)
