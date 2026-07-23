"""Public intent module boundaries stay stable while the legacy CLI remains."""


def test_lifecycle_boundary_exports_state_transitions():
    from core import intent_lifecycle

    assert callable(intent_lifecycle.create_intent)
    assert callable(intent_lifecycle.mark_triggered)
    assert callable(intent_lifecycle.mark_executed)
    assert callable(intent_lifecycle.cancel_intent)


def test_scheduler_boundary_exports_due_and_reconciliation_operations():
    from core import intent_scheduler

    assert callable(intent_scheduler.get_due_intents)
    assert callable(intent_scheduler.write_inflight)
    assert callable(intent_scheduler.reconcile_inflight)
    assert callable(intent_scheduler.peek_breaches)


def test_closure_boundary_is_the_single_user_closure_entry():
    from core import intent_closure

    assert callable(intent_closure.record_closure)
    assert callable(intent_closure.generate_closure_reask_intents)
