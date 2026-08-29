package com.zmm.presence

/**
 * Which setup step on [PairActivity] is open, and when finishing one should
 * open the next.
 *
 * Pure state, deliberately: this is the part of the accordion with a wrong
 * answer, and every wrong answer is invisible on a phone until you happen to
 * retrace the exact path that produces it. Kept out of the Activity so the
 * paths can be walked in a JVM test — the same reasoning that put
 * [Prefs.isPublicUrl] behind a unit test rather than a device.
 */
class StepAccordion {

    enum class Step { HUB, PERMISSIONS, GEOFENCE, DRIVE }

    sealed interface Expansion {
        /** Follow the steps: open the first unfinished one. */
        object Auto : Expansion
        /** Everything closed, by the user's hand. */
        object None : Expansion
        data class Open(val step: Step) : Expansion
    }

    var expansion: Expansion = Expansion.Auto
        private set

    /** What [resolve] last opened; [toggle] closes against it. */
    var openStep: Step? = null
        private set

    /**
     * Done-ness as of the previous [resolve], so a step *finishing* can be told
     * from a step that was already finished when it was opened.
     */
    private var lastDone: Map<Step, Boolean> = emptyMap()

    /** A header tap: open this step, or close it if it is already the open one. */
    fun toggle(step: Step) {
        expansion = if (openStep == step) Expansion.None else Expansion.Open(step)
    }

    /**
     * Decide what should be open now.
     *
     * [blocked] is for a step that cannot be completed from this screen at all
     * — drive mode against a LAN-only hub, whose only control is disabled by
     * design. Blocked steps are never auto-advanced onto, because opening a
     * dead control is how an accordion strands someone; a header tap still
     * opens one, so the explanation inside stays reachable.
     */
    fun resolve(done: Map<Step, Boolean>, blocked: Set<Step> = emptySet()): Step? {
        // The TRANSITION, not the state. Reopening an already-done step to
        // change something — a new hub URL, a different car — would otherwise
        // satisfy this on the very first resolve and snap shut in your hand.
        (expansion as? Expansion.Open)?.let {
            if (lastDone[it.step] == false && done[it.step] == true) {
                expansion = Expansion.Auto
            }
        }
        lastDone = done

        openStep = when (val e = expansion) {
            is Expansion.Auto -> Step.values().firstOrNull { done[it] == false && it !in blocked }
            is Expansion.None -> null
            is Expansion.Open -> e.step
        }
        return openStep
    }
}
