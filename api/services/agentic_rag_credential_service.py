"""Per-account bring-your-own-key LLM credentials for Prism.

Scoped to predefined-model providers only (today: OpenAI and OpenRouter --
whichever plugins are actually installed with a fixed model catalog).
Customizable-model providers (Ollama, vLLM, HuggingFace Hub, OpenAI-API-
compatible) have no fixed model list at all -- using one means first
registering a specific model+endpoint, a materially different (and
larger) UI than "pick a model, paste a key", so that's left for later
rather than half-built now. The moment a new predefined-model provider
plugin is installed, it shows up here automatically -- nothing here is
hardcoded to OpenAI/OpenRouter by name.
"""

import json
from typing import Any

from core.helper.encrypter import decrypt_token, encrypt_token
from core.model_manager import ModelInstance
from core.provider_manager import ProviderManager
from dify_graph.model_runtime.entities.model_entities import ModelType
from dify_graph.model_runtime.entities.provider_entities import FormType
from extensions.ext_database import db
from models import Account
from models.agentic_rag import PrismUserLlmCredential


def _get_credential_schema(tenant_id: str, provider: str):
    configurations = ProviderManager().get_configurations(tenant_id)
    configuration = configurations.configurations.get(provider)
    if configuration is None:
        return None
    return configuration.provider.provider_credential_schema


def _secret_variable_names(schema) -> set[str]:
    if schema is None:
        return set()
    return {f.variable for f in schema.credential_form_schemas if f.type == FormType.SECRET_INPUT}


def list_supported_providers(tenant_id: str) -> list[dict[str, Any]]:
    """Every predefined-model LLM provider this deployment's plugins make
    available, each with the exact field list (name/label/type/required)
    its own provider_credential_schema declares -- so the credential form
    is generated from the provider's own definition, not hand-mapped per
    provider one at a time.
    """
    configurations = ProviderManager().get_configurations(tenant_id)
    models = configurations.get_models(model_type=ModelType.LLM, only_active=False)
    # get_models() returns a lightweight SimpleModelProviderEntity with no
    # credential_schema attached -- only used here to find which provider
    # names actually have a usable (predefined) model catalog at all; the
    # real provider entity, with its schema, comes from .configurations.
    predefined_provider_names = {m.provider.provider for m in models}

    providers: dict[str, dict[str, Any]] = {}
    for provider_name in predefined_provider_names:
        configuration = configurations.configurations.get(provider_name)
        if configuration is None:
            continue
        provider = configuration.provider
        schema = provider.provider_credential_schema
        if not schema:
            continue
        providers[provider_name] = {
            "provider": provider_name,
            "label": provider.label.en_US,
            "fields": [
                {
                    "variable": field.variable,
                    "label": field.label.en_US,
                    "type": field.type.value,
                    "required": field.required,
                    "placeholder": field.placeholder.en_US if field.placeholder else None,
                    "secret": field.type == FormType.SECRET_INPUT,
                }
                for field in schema.credential_form_schemas
            ],
        }
    return list(providers.values())


def _row_for(account_id: str, provider: str) -> PrismUserLlmCredential | None:
    return (
        db.session.query(PrismUserLlmCredential)
        .filter(
            PrismUserLlmCredential.account_id == account_id,
            PrismUserLlmCredential.provider == provider,
        )
        .first()
    )


def get_credential_status(account: Account, tenant_id: str) -> dict[str, dict[str, Any]]:
    """Which providers this account has BYOK set up for, with secret
    fields redacted (last 4 characters only, same spirit as Dify's own
    obfuscated_token) -- never returns a usable raw key.
    """
    rows = db.session.query(PrismUserLlmCredential).filter(PrismUserLlmCredential.account_id == account.id).all()
    status: dict[str, dict[str, Any]] = {}
    for row in rows:
        secret_vars = _secret_variable_names(_get_credential_schema(tenant_id, row.provider))
        config = json.loads(row.encrypted_config)
        redacted = {}
        for key, value in config.items():
            if key in secret_vars:
                plain = decrypt_token(tenant_id=tenant_id, token=value)
                redacted[key] = f"...{plain[-4:]}" if len(plain) > 4 else "...."
            else:
                redacted[key] = value
        status[row.provider] = {"configured": True, "fields": redacted}
    return status


def save_credential(account: Account, tenant_id: str, provider: str, fields: dict[str, str]) -> None:
    schema = _get_credential_schema(tenant_id, provider)
    if schema is None:
        raise ValueError(f"{provider!r} is not a supported provider")

    known_vars = {f.variable for f in schema.credential_form_schemas}
    for field in schema.credential_form_schemas:
        if field.required and not (fields.get(field.variable) or "").strip():
            raise ValueError(f"{field.label.en_US} is required")
    # Drop anything the schema doesn't recognize rather than storing it.
    clean_fields = {k: v for k, v in fields.items() if k in known_vars}

    encrypted_fields = {}
    for field in schema.credential_form_schemas:
        value = clean_fields.get(field.variable)
        if value is None:
            continue
        encrypted_fields[field.variable] = (
            encrypt_token(tenant_id, value) if field.type == FormType.SECRET_INPUT else value
        )

    row = _row_for(account.id, provider)
    if row is None:
        row = PrismUserLlmCredential(account_id=account.id, provider=provider, encrypted_config="{}")
        db.session.add(row)
    row.encrypted_config = json.dumps(encrypted_fields)
    db.session.commit()


def clear_credential(account: Account, provider: str) -> bool:
    row = _row_for(account.id, provider)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def resolve_credentials_override(account: Account, tenant_id: str, provider: str) -> dict[str, str] | None:
    """The decrypted credentials dict for model_instance.credentials, or
    None if this account has no BYOK set up for this provider -- the
    caller falls back to the tenant's own shared credential in that case.
    """
    row = _row_for(account.id, provider)
    if row is None:
        return None
    secret_vars = _secret_variable_names(_get_credential_schema(tenant_id, provider))
    config = json.loads(row.encrypted_config)
    return {
        key: (decrypt_token(tenant_id=tenant_id, token=value) if key in secret_vars else value)
        for key, value in config.items()
    }


def build_model_instance(tenant_id: str, provider: str, model_name: str, credentials: dict[str, str]) -> ModelInstance:
    """A ModelInstance that uses exactly `credentials`, for a provider the
    tenant may have never configured at all -- ModelManager.get_model_instance
    can't do this: it raises ProviderTokenNotInitError before returning
    anything, because ModelInstance.__init__ fetches the TENANT's own
    stored credentials unconditionally (confirmed live: calling it for
    langgenius/openai/openai on a tenant with no OpenAI Provider row raises
    immediately, with no chance to override .credentials afterward).

    get_provider_model_bundle, one level down, has no such requirement --
    it only needs the provider's plugin to be installed, not configured.
    So this builds the bundle that way, then constructs ModelInstance via
    object.__new__ to skip __init__'s credential fetch entirely, setting
    the same attributes __init__ would have (confirmed by reading its
    source) except load_balancing_manager, deliberately left None so
    invoke_llm's round-robin wrapper takes its no-load-balancer path: call
    the model straight through with exactly the given credentials, no
    rotation across other keys. Verified live end-to-end: this construction
    made a real call to OpenAI's API using a (deliberately invalid) test
    key and got back a genuine InvokeAuthorizationError from OpenAI itself,
    not an internal error -- the plumbing is real, not just type-correct.
    """
    bundle = ProviderManager().get_provider_model_bundle(tenant_id, provider, ModelType.LLM)
    instance = object.__new__(ModelInstance)
    instance.provider_model_bundle = bundle
    instance.model_name = model_name
    instance.provider = bundle.configuration.provider.provider
    instance.credentials = credentials
    instance.parameters = {}
    instance.stop = ()
    instance.model_type_instance = bundle.model_type_instance
    instance.load_balancing_manager = None
    return instance
