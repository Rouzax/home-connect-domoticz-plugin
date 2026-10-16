"""Stand-ins for the worker threads, so plugin tests drive every message by hand."""

import workers


class FakeRequestWorker:
    def __init__(self, client, budget_, results, generation, lang):
        self.client = client
        self.results = results
        self.generation = generation
        self.jobs = []
        self.token = None
        self.auth_generation = 0
        self.parked = False
        self.stopped = False
        self.accepting = True
        self.state = "running"
        self.name = "hc-request"

    def start(self):
        pass

    def submit(self, job):
        if not self.accepting:
            return False
        self.jobs.append(job)
        return True

    def set_token(self, token, auth_generation):
        self.token, self.auth_generation = token, auth_generation

    def park(self):
        self.parked = True

    def unpark(self):
        self.parked = False

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return not self.stopped and self.state != "crashed"

    def kinds(self):
        return [j.kind for j in self.jobs]

    def take(self, kind):
        found = [j for j in self.jobs if j.kind == kind]
        assert found, f"no {kind} job in {self.kinds()}"
        self.jobs.remove(found[-1])
        return found[-1]


class FakeStreamWorker(FakeRequestWorker):
    def __init__(self, *args):
        super().__init__(*args)
        self.name = "hc-stream"

    def set_token(self, token):
        self.token = token


def result(worker, job, status, body=b"", category="", error_key="", retry_after=None):
    return workers.Result(worker.generation, job, status, body, category, error_key, retry_after, 5)


def push(worker, message):
    worker.results.put(message)


class NoClient:
    def abort(self):
        pass
