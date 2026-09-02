# Known debt

Pre-existing issues, not owned by current capstone work. Fix only when touching the file for another reason.

- `OfficialA2AServicer.SubscribeToTask`: terminal frame missed when task flips between yield and check. Fix: emit terminal frame before break. See `test_official_subscription_resumes_waiting_graph_and_maps_cancel`.
