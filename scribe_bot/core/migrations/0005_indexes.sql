-- Indexes for the lookups every page makes (page-speed budget: under 1 s on the M14 with a year or two of data).
CREATE INDEX task_owner_status ON task(owner_person_id, status);
CREATE INDEX task_conversation ON task(conversation_id);
CREATE INDEX decision_conversation ON decision(conversation_id);
CREATE INDEX fact_person ON person_fact(person_id);
CREATE INDEX fact_conversation ON person_fact(conversation_id);
CREATE INDEX speaker_person ON speaker(person_id);
