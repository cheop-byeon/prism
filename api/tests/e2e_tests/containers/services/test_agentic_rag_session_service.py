"""Integration tests for services/agentic_rag_session_service.py's
get_owned_tenant_id -- the tenant-resolution primitive Prism's
bring-your-own-key feature depends on (ask_decision_agent.ask()'s
model_tenant_id): resolving the wrong tenant here would mean either
silently using someone else's credentials or never finding a real,
already-configured one, so it's tested against real Account/Tenant/
TenantAccountJoin rows rather than trusted on inspection alone.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.account import Account, Tenant, TenantAccountJoin, TenantAccountRole
from services.agentic_rag_session_service import get_data_tenant_id, get_owned_tenant_id


class AgenticRagTestDataFactory:
    @staticmethod
    def create_account(db_session_with_containers: Session) -> Account:
        account = Account(
            email=f"{uuid4()}@example.com",
            name=f"user-{uuid4()}",
            interface_language="en-US",
            status="active",
        )
        db_session_with_containers.add(account)
        db_session_with_containers.commit()
        return account

    @staticmethod
    def create_tenant(db_session_with_containers: Session) -> Tenant:
        tenant = Tenant(name=f"tenant-{uuid4()}", status="normal")
        db_session_with_containers.add(tenant)
        db_session_with_containers.commit()
        return tenant

    @staticmethod
    def join(
        db_session_with_containers: Session, *, account: Account, tenant: Tenant, role: TenantAccountRole
    ) -> None:
        db_session_with_containers.add(
            TenantAccountJoin(tenant_id=tenant.id, account_id=account.id, role=role, current=True)
        )
        db_session_with_containers.commit()


class TestGetOwnedTenantId:
    def test_owner_resolves_to_their_own_tenant(self, db_session_with_containers: Session):
        account = AgenticRagTestDataFactory.create_account(db_session_with_containers)
        tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        AgenticRagTestDataFactory.join(
            db_session_with_containers, account=account, tenant=tenant, role=TenantAccountRole.OWNER
        )

        assert get_owned_tenant_id(account) == tenant.id

    def test_account_with_no_tenant_at_all_returns_none(self, db_session_with_containers: Session):
        account = AgenticRagTestDataFactory.create_account(db_session_with_containers)

        assert get_owned_tenant_id(account) is None

    def test_normal_member_who_never_owns_a_tenant_returns_none(self, db_session_with_containers: Session):
        # The invited-collaborator case: someone added to the shared admin
        # tenant as a plain member, who never self-registered a workspace
        # of their own -- there's no personal tenant to point BYOK at.
        account = AgenticRagTestDataFactory.create_account(db_session_with_containers)
        admin_tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        AgenticRagTestDataFactory.join(
            db_session_with_containers, account=account, tenant=admin_tenant, role=TenantAccountRole.NORMAL
        )

        assert get_owned_tenant_id(account) is None

    def test_owner_who_is_also_invited_elsewhere_resolves_to_their_own_tenant(
        self, db_session_with_containers: Session
    ):
        # A self-service account that later gets invited into the shared
        # admin tenant as a normal member ends up with two TenantAccountJoin
        # rows -- this must resolve to the one they actually own, not the
        # invited one, even though the invited join exists too.
        account = AgenticRagTestDataFactory.create_account(db_session_with_containers)
        own_tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        admin_tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        AgenticRagTestDataFactory.join(
            db_session_with_containers, account=account, tenant=own_tenant, role=TenantAccountRole.OWNER
        )
        AgenticRagTestDataFactory.join(
            db_session_with_containers, account=account, tenant=admin_tenant, role=TenantAccountRole.NORMAL
        )

        assert get_owned_tenant_id(account) == own_tenant.id

    def test_admin_role_alone_does_not_count_as_owner(self, db_session_with_containers: Session):
        # Dify distinguishes "owner" from "admin" as TenantAccountRole
        # values -- get_owned_tenant_id intentionally checks only for
        # owner, since that's the role a self-service sign-up actually
        # gets on their own tenant (RegisterService.register ->
        # TenantService.create_tenant), not admin.
        account = AgenticRagTestDataFactory.create_account(db_session_with_containers)
        tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        AgenticRagTestDataFactory.join(
            db_session_with_containers, account=account, tenant=tenant, role=TenantAccountRole.ADMIN
        )

        assert get_owned_tenant_id(account) is None


class TestGetDataTenantId:
    # The integration DB is shared across tests and never cleaned, so each
    # test backdates the tenant it expects to win to just before whatever
    # is already there, rather than assuming the table starts empty.
    @staticmethod
    def _create_tenant_older_than_all(db_session_with_containers: Session) -> Tenant:
        oldest_existing = db_session_with_containers.query(func.min(Tenant.created_at)).scalar()
        tenant = AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        if oldest_existing is not None:
            tenant.created_at = oldest_existing - timedelta(days=1)
            db_session_with_containers.commit()
        return tenant

    def test_resolves_to_the_oldest_tenant(self, db_session_with_containers: Session):
        setup_tenant = self._create_tenant_older_than_all(db_session_with_containers)
        AgenticRagTestDataFactory.create_tenant(db_session_with_containers)

        assert get_data_tenant_id() == setup_tenant.id

    def test_ignores_insertion_order(self, db_session_with_containers: Session):
        # A newer sign-up workspace whose row happens to be inserted (and so
        # physically stored) before the setup workspace must not win -- the
        # unordered Tenant.first() this replaces could return either.
        AgenticRagTestDataFactory.create_tenant(db_session_with_containers)
        setup_tenant = self._create_tenant_older_than_all(db_session_with_containers)

        assert get_data_tenant_id() == setup_tenant.id
