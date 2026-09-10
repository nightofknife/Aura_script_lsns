"""Pre-resolved service dependencies honor only the current plan's replacement."""
from types import SimpleNamespace

from packages.aura_core.api.definitions import ServiceDefinition
from packages.aura_core.api.registries import ServiceRegistry
from packages.aura_core.context.plan import current_plan_name


class Base:
    pass


class Replacement(Base):
    pass


def registry():
    result = ServiceRegistry()
    for fqid, cls, replace in (
        ('plans/base/app', Base, None),
        ('plans/example/app', Replacement, 'plans/base/app'),
    ):
        package = SimpleNamespace(canonical_id='@' + fqid.rsplit('/', 1)[0])
        result.register(ServiceDefinition(
            alias='app', fqid=fqid, service_class=cls,
            plugin=SimpleNamespace(package=package), public=True, replace=replace))
    return result


def test_explicit_dependency_redirects_only_in_replacing_plan():
    services = registry()
    token = current_plan_name.set(None)
    try:
        original = services.get_service_instance('plans/base/app')
        assert type(original) is Base
        current_plan_name.set('example')
        replacement = services.get_service_instance('plans/base/app')
        assert type(replacement) is Replacement
        assert replacement is services.get_service_instance('app')
        current_plan_name.set('other')
        assert services.get_service_instance('plans/base/app') is original
    finally:
        current_plan_name.reset(token)


def test_unrelated_same_alias_is_not_redirected():
    services = registry()
    token = current_plan_name.set('example')
    try:
        unrelated = ServiceDefinition(alias='different', fqid='plans/base/different',
                                      service_class=Base, plugin=SimpleNamespace(
                                          package=SimpleNamespace(canonical_id='@plans/base')), public=True)
        services.register(unrelated)
        assert type(services.get_service_instance('plans/base/different')) is Base
    finally:
        current_plan_name.reset(token)
