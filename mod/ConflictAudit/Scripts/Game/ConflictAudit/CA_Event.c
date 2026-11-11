// Original observation code. API reference: Bohemia Script-Diff 1.9.0.37.
// No RPC receiver, remote commands, kicks, bans or entity deletion.
class CA_Settings
{
	// Conservative starting limits; measure on a staging server before deployment.
	static const int TICK_MS = 1000;
	static const int STARTUP_GRACE_MS = 60000;
	static const int MAX_QUEUE = 1024;
	static const int MAX_EVENTS_PER_TICK = 256;
	static const int FILE_BYTES = 8388608;
	static const int MAX_FILES_PER_SESSION = 8;
	static const int CELL_METERS = 150;
	static const int MAX_CELLS = 64;
	static const int MAX_QUERY_ENTITIES = 2000;
	static const int QUERY_BUDGET_MS = 3;
	static const int REBASE_AFTER_MS = 120000;
	static const bool SCANNER_ENABLED = true;
}

class CA_Event
{
	int seq;
	int t_ms;
	int utc;
	string kind;
	string source;
	string entity;
	string parent;
	string prefab;
	string category;
	vector position;
	int actor_id;
	string actor_uid;
	string actor_name;
	string attribution = "unknown";
	string source_entity;
	float amount;
	int count;
	bool warmup;

	// Temporary weak engine reference, never serialized. Used to obtain the final
	// transform after spawning completes. Damage positions remain event-time values.
	IEntity deferredEntity;

	string Serialize(string session)
	{
		JsonSaveContext json = new JsonSaveContext();
		json.WriteValue("schema", 1);
		json.WriteValue("session", session);
		json.WriteValue("seq", seq);
		json.WriteValue("t_ms", t_ms);
		json.WriteValue("utc", utc);
		json.WriteValue("kind", kind);
		json.WriteValue("source", source);
		json.WriteValue("entity", entity);
		json.WriteValue("parent", parent);
		json.WriteValue("prefab", prefab);
		json.WriteValue("category", category);
		json.WriteValue("x", position[0]);
		json.WriteValue("y", position[1]);
		json.WriteValue("z", position[2]);
		json.WriteValue("actor_id", actor_id);
		json.WriteValue("actor_uid", actor_uid);
		json.WriteValue("actor_name", actor_name);
		json.WriteValue("attribution", attribution);
		json.WriteValue("source_entity", source_entity);
		json.WriteValue("amount", amount);
		json.WriteValue("count", count);
		json.WriteValue("warmup", warmup);
		string line = json.SaveToString();
		// The serializer escapes control characters inside strings. Remove only
		// formatting newlines, so one event occupies one physical line.
		line.Replace("\r", "");
		line.Replace("\n", "");
		return line;
	}
}

class CA_Cell
{
	string key;
	int lastScan;
	ref set<string> ids = new set<string>();
}
