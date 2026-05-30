from datetime import timedelta

from dischat.jobs.queue import DeliveryJob, schedule_retry


def test_schedule_retry_marks_job_failed_and_increments_attempts() -> None:
    job = DeliveryJob(
        id=1, event_id=1, target_type="room", target_mxid=None, matrix_room_id="!room:test"
    )

    updated = schedule_retry(job, error="boom", delay=timedelta(seconds=30))

    assert updated.status == "failed"
    assert updated.attempts == 1
    assert updated.last_error == "boom"
