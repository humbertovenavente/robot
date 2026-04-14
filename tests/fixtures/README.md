# Test Fixtures

Optional test-image fixtures go here. Unit tests in this phase avoid fixture dependencies
so they run without a trained model. Fixtures are useful for Plan 05 integration-testing
when a real `best.pt` exists.

Suggested contents once available:
- `black.jpg` — all-black 640x480 frame (should produce no detection)
- `package_a.jpg` — a real captured frame with class A package for smoke tests
