# Correctness review

Review changed behavior for concrete functional defects: incorrect conditions, branches, return paths, state changes, values, types, ordering, and boundary cases. Trace changed data through nearby callers and consumers. Check null, empty, duplicate, and unexpected inputs when relevant. Report only defects introduced or made worse by this change; avoid style preferences and speculative edge cases.
