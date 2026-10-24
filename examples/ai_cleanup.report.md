# Under the bed: 7 thing(s) to ask about

Source: examples/ai_cleanup.diff. Paste these back to the assistant, one by one.

## billing/invoice.py

- [ ] **L10 flag-parameter**: `total` grew a flag `skip_tax`. Which callers pass it, and why is this not two functions, or a fix of the one?
  `skip_tax=False`
- [ ] **L14 swallowed-exception**: Why is this error swallowed? What should happen when it fires, and where will anyone see it?
  `except Exception:`
- [ ] **L16 commented-out-code**: Why is dead code kept in a comment? Git remembers it. Can it go?
  `# tax = tax_for(region, amount) * 1.0`
- [ ] **L17 todo**: Who does this TODO, and when? If it is needed, why is it not done in this change?
  `# TODO: figure out why tax_for fails for EU`
- [ ] **L18 silenced-check**: Which warning does this silence, and why is hiding it better than fixing it?
  `return amount + tax  # type: ignore`

## tests/test_invoice.py

- [ ] **L20 weakened-test**: Why is this test switched off? Is the code it guards broken now?
  `@pytest.mark.skip(reason="flaky")`
- [ ] **L21 weakened-test**: This change removes 2 assertion(s) and adds 1. What is no longer checked, and why is that safe?
  `assert total(LINES, "EU") == 121`
