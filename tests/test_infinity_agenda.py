"""The floor session's own open lessons are an agenda and must wake it.

The per-session self-prompt queue skips the floor slug, and the floor wake
posts on house pressure. An open lesson on that session still has to wake it.
"""
from test_autonomy import deliver, messages, open_manager


def test_an_open_lesson_on_the_floor_session_wakes_it(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['Looking at the lesson.'], self_prompt_gap_seconds=0, min_wake_gap_seconds=0)
    floor = manager.create('Floor')['slug']
    manager.autonomy.settings.infinity_slug = floor
    manager.infrastructure.status = lambda: {'hosts': [{'label': 'node-a', 'ordinal': 1, 'health': 'healthy'}]}
    with manager:
        manager.autonomy.learning.record_lesson(
            floor, 'The retired hook is a ghost setting.', 'Hooks still point at the retired MCP.',
            'No hook names the retired companion.')
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, floor)
        wakes = [item for item in messages(manager, floor) if item.meta.get('wake')]
        assert len(wakes) == 1, f'floor session was never woken; wakes={wakes}'
        assert 'Standing agenda is due.' in wakes[0].text
        assert 'The retired hook is a ghost setting.' in wakes[0].text
        assert wakes[0].meta['wake']['events'] == ['self_prompt']
