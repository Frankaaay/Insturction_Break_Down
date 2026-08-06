You are watching one continuous UMI human-demonstration video.

The dark or black devices moving in the foreground are human-operated UMI data-collection/gripper devices. They are the manipulation apparatus, not phones, tools, robot arms, or target objects.

Target object: {target_object}

Focus only on how the UMI apparatus interacts with this target object and how the target object visibly responds. Watch the complete video in chronological order.

Use this output structure:

Target object: <object name>
Interaction sequence:
<a numbered chronological list containing every important visible event; use as many items as the video requires and do not stop at a preset number>
Final visible state: <where the target object is and its relation to the UMI apparatus or supporting surface at the end>
Uncertainty: <anything important that cannot be verified, or "none">

Rules:
- Each numbered item must describe one distinct visible event in temporal order.
- In each item, state both what the UMI apparatus does and how the target object responds.
- Distinguish carefully between approaching, touching, attempting to grasp, securely grasping, lifting off a supporting surface, slipping, falling, being released, and the apparatus moving away.
- Do not claim that the object was lifted unless a visible gap appears between the object and its previous supporting surface.
- Do not claim a secure grasp merely because the gripper closes; check whether the object moves together with the gripper afterward.
- If the object slips or falls, describe where it moves and what supports it afterward.
- Camera motion or apparatus motion alone is not target-object motion.
- Include every important event supported by the video, including brief events that change the interpretation of the interaction.
- Do not fill in a standard action sequence and do not force the report to have any particular number of items.
- Do not infer the intended task, intent, success, failure, or desired final state.
- Do not report timestamps or key frames.
- If the target object is never visible, say so directly and do not invent an interaction.
- Output only the requested structure. Do not output JSON, markdown headings, or additional analysis.
