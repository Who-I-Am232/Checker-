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
	static const bool MOVEMENT_ENABLED = true;
	static const int MAX_MOVEMENT_PLAYERS = 256;
	static const int MOVEMENT_GRACE_MS = 10000;
	static const int MOVEMENT_MAX_GAP_MS = 2500;
	static const int MAX_GM_SOURCES = 1024;
	static const int GM_SOURCE_LIFETIME_MS = 60000;
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
	string actor_role = "UNKNOWN";
	string attribution = "unknown";
	string source_entity;
	float amount;
	int count;
	bool warmup;
	string motion_mode;
	vector from_position;
	int delta_ms;
	float distance_m;
	float avg_speed_mps;
	float inst_speed_mps;
	bool inst_speed_valid;

	// Temporary weak engine reference, never serialized. Used to obtain the final
	// transform after spawning completes. Damage positions remain event-time values.
	IEntity deferredEntity;
	ref CA_GmSource authority;
	ref array<IEntity> permissionCandidates;

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
		json.WriteValue("actor_role", actor_role);
		json.WriteValue("attribution", attribution);
		json.WriteValue("source_entity", source_entity);
		json.WriteValue("amount", amount);
		json.WriteValue("count", count);
		json.WriteValue("warmup", warmup);
		if (authority)
		{
			string permission = "gm_authorized";
			// Creating an object as GM does not authorize all its future uses.
			if ((kind == "explosive_damage" || kind == "explosive_trigger") && authority.basis == "gm_editor")
				permission = "gm_origin";
			json.WriteValue("authorization", permission);
			json.WriteValue("authorization_id", authority.actionID);
			json.WriteValue("authorization_source", authority.basis);
			json.WriteValue("authorization_entity", authority.rootID);
			json.WriteValue("authorized_actor_id", authority.actorID);
			json.WriteValue("authorized_actor_uid", authority.uid);
			json.WriteValue("authorized_actor_name", authority.name);
			json.WriteValue("authorized_actor_role", authority.role);
		}
		if (kind == "movement_candidate")
		{
			json.WriteValue("motion_mode", motion_mode);
			json.WriteValue("from_x", from_position[0]);
			json.WriteValue("from_y", from_position[1]);
			json.WriteValue("from_z", from_position[2]);
			json.WriteValue("delta_ms", delta_ms);
			json.WriteValue("distance_m", distance_m);
			json.WriteValue("avg_speed_mps", avg_speed_mps);
			json.WriteValue("inst_speed_mps", inst_speed_mps);
			json.WriteValue("inst_speed_valid", inst_speed_valid);
		}
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
