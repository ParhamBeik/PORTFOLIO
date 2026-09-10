import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";

// testing-library polls `waitFor` for 1000 ms by default, which is a wall-clock
// budget on a machine whose speed is not part of what these tests assert. Under
// CPU contention -- a CI runner, or a backend suite running alongside this one
// -- a promise rejection plus its state flush can miss it, and the failure
// lands on whichever spec happened to be scheduled unluckily rather than on a
// spec that is wrong. Observed here: the same run reported Dashboard failing,
// then useApi, with no change in between.
//
// This does not weaken an assertion. A correct component still settles in
// milliseconds and the test still passes immediately; only a slow machine gets
// the extra room. A genuinely stuck update still fails, five seconds later.
configure({ asyncUtilTimeout: 5000 });
