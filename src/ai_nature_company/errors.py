"""Stable domain error types exposed by the substrate."""


class SubstrateError(Exception):
    """Base class for expected, safe-to-report substrate failures."""

    code = "substrate_error"


class AuthorizationError(SubstrateError):
    code = "forbidden"


class ConflictError(SubstrateError):
    code = "conflict"


class NotFoundError(SubstrateError):
    code = "not_found"


class ValidationError(SubstrateError):
    code = "invalid_operation"


class BudgetError(SubstrateError):
    code = "budget_exceeded"


class LeaseError(SubstrateError):
    code = "invalid_lease"


class SeparationOfDutiesError(SubstrateError):
    code = "separation_of_duties"


class InternalError(SubstrateError):
    """Unexpected failure. Safe to report the code; log the traceback server-side."""

    code = "internal_error"
