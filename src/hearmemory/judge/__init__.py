"""Judge layer: generic extractor v1, rule judge, optional Jev judge, async worker.

Entry points (interfaces.ENTRY_POINTS): project_index.ProjectIndex, extract.Extractor, templates.TEMPLATES,
jev.JevJudge, jev.jev_capability, rules.RuleJudge, scope.scope_facts, worker.run_pipeline,
worker.spawn_background, worker.stop_worker. Importing this package has no side effects and does not import
the optional typesafe_sdk.
"""
