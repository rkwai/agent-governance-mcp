"""Function-scoped black-box company fixtures."""

from __future__ import annotations

import pytest

from .scenario import Scenario


@pytest.fixture
def scenario() -> Scenario:
    return Scenario()


@pytest.fixture
async def bootstrapped(scenario: Scenario) -> Scenario:
    await scenario.bootstrap()
    return scenario


@pytest.fixture
async def offer_cell(scenario: Scenario) -> Scenario:
    await scenario.build_offer_cell()
    return scenario


@pytest.fixture
async def running_experiment(scenario: Scenario) -> Scenario:
    await scenario.build_running_experiment()
    return scenario


@pytest.fixture
async def evaluating_experiment(scenario: Scenario) -> Scenario:
    await scenario.build_evaluating_experiment()
    return scenario


@pytest.fixture
async def evaluated_experiment(scenario: Scenario) -> Scenario:
    await scenario.build_evaluated_experiment()
    return scenario


@pytest.fixture
async def allocated_company(scenario: Scenario) -> Scenario:
    await scenario.build_allocation()
    return scenario
