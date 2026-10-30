"""A decreasing reflection frequency as the number of observed messages grows."""
import math

SLOTS = {'style', 'interests', 'humor', 'conversation_habits', 'group_culture', 'curiosity'}


def update_frequency(total_messages):
    """From 1/12 towards 1/120; the first ~1000 messages settle the character."""
    return 1/120 + (1/12 - 1/120) * math.exp(-max(0, total_messages)/600)


def messages_per_review(total_messages):
    return math.ceil(1/update_frequency(total_messages))


def review_interval(total_messages):
    return 600 + 2100 * (1-math.exp(-max(0,total_messages)/600))


def learning_phase(total_messages):
    return 'getting_acquainted' if total_messages<200 else 'settling' if total_messages<1200 else 'stable'


def question_gap(total_messages):
    return math.ceil(20 + 100 * (1-math.exp(-max(0,total_messages)/400)))
