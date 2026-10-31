# Hold source claim through durable writes

Source-runner writes now settle before cancellation can release their runner claim. This prevents a late write from affecting a later owner of the same job; conditional store updates remain a second fence for already-orphaned writes.
