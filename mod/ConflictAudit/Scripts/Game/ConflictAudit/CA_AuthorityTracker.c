// Server-only provenance. A role is not a blanket exemption for that player.
class CA_GmSource
{
	IEntity source;
	string actionID;
	string rootID;
	string basis;
	int actorID;
	string uid;
	string name;
	string role;
	int createdAt;
	int expiresAt;
}

class CA_AuthorityTracker
{
	protected ref map<string, ref CA_GmSource> m_Sources = new map<string, ref CA_GmSource>();
	protected int m_ActionCounter;

	static string Role(int playerID)
	{
		if (playerID <= 0)
			return "UNKNOWN";
		if (SCR_Global.IsAdmin(playerID))
			return "ADMIN";
		if (GetGame().GetPlayerManager().HasPlayerRole(playerID, EPlayerRole.GAME_MASTER))
			return "GAME_MASTER";
		return "PLAYER";
	}

	void Sweep(int now)
	{
		array<string> expired = {};
		foreach (string key, CA_GmSource context : m_Sources)
		{
			if (!context.source || context.expiresAt < now)
				expired.Insert(key);
		}
		foreach (string keyToRemove : expired)
			m_Sources.Remove(keyToRemove);
	}

	// Called after the successful server placement callback, never from an RPC.
	CA_GmSource Register(CA_Runtime runtime, IEntity entity, SCR_EditorManagerEntity manager, SCR_EditorModeEntity mode)
	{
		if (!Replication.IsServer() || !entity || !manager || !mode)
			return null;
		if (entity.GetWorld() != GetGame().GetWorld())
			return null;
		if (mode.GetModeType() != EEditorMode.EDIT || manager.FindModeEntity(EEditorMode.EDIT) != mode)
			return null;
		if (mode.IsLimited() || manager.IsLimited())
			return null;
		int playerID = manager.GetPlayerID();
		string role = Role(playerID);
		string uid = runtime.GetAuditedIdentity(playerID);
		if (uid.IsEmpty() || (role != "ADMIN" && role != "GAME_MASTER"))
			return null;
		if (m_Sources.Count() >= CA_Settings.MAX_GM_SOURCES)
		{
			CA_Event loss = runtime.NewEvent("telemetry_loss", "gm_source_limit");
			loss.count = 1;
			runtime.Enqueue(loss);
			return null;
		}
		CA_GmSource context = new CA_GmSource();
		context.source = entity;
		context.rootID = CA_Runtime.EntityKey(entity);
		context.actionID = string.Format("gm-%1-%2", runtime.Now(), ++m_ActionCounter);
		context.basis = "gm_editor";
		context.actorID = playerID;
		context.uid = uid;
		context.name = GetGame().GetPlayerManager().GetPlayerName(playerID);
		context.role = role;
		context.createdAt = runtime.Now();
		context.expiresAt = context.createdAt + CA_Settings.GM_SOURCE_LIFETIME_MS;
		m_Sources.Set(context.rootID, context);
		return context;
	}

	// Optional server adapter for an FX generator's exact child projectile.
	// No time/position/radius matching and no extension of the original lifetime.
	bool Link(CA_Runtime runtime, IEntity child, IEntity source)
	{
		if (!Replication.IsServer() || !child || !source)
			return false;
		if (child.GetWorld() != GetGame().GetWorld() || source.GetWorld() != GetGame().GetWorld())
			return false;
		CA_GmSource parent = m_Sources.Get(CA_Runtime.EntityKey(source));
		if (!parent || parent.source != source || parent.expiresAt < runtime.Now())
			return false;
		if (m_Sources.Count() >= CA_Settings.MAX_GM_SOURCES)
		{
			CA_Event loss = runtime.NewEvent("telemetry_loss", "gm_source_limit");
			loss.count = 1;
			runtime.Enqueue(loss);
			return false;
		}
		CA_GmSource context = new CA_GmSource();
		context.source = child;
		context.rootID = parent.rootID;
		context.actionID = parent.actionID;
		context.basis = "gm_effect_link";
		context.actorID = parent.actorID;
		context.uid = parent.uid;
		context.name = parent.name;
		context.role = parent.role;
		context.createdAt = runtime.Now();
		context.expiresAt = parent.expiresAt;
		m_Sources.Set(CA_Runtime.EntityKey(child), context);
		return true;
	}

	void Capture(CA_Event event, IEntity source)
	{
		event.permissionCandidates = {};
		// Preserve the observed ancestry, not a possibly changed parent at flush.
		for (int depth = 0; source && depth < 8; depth++)
		{
			event.permissionCandidates.Insert(source);
			source = source.GetParent();
		}
		Resolve(event);
	}

	void Resolve(CA_Event event)
	{
		if (event.authority || !event.permissionCandidates)
			return;
		foreach (IEntity source : event.permissionCandidates)
		{
			if (!source)
				continue;
			CA_GmSource context = m_Sources.Get(CA_Runtime.EntityKey(source));
			if (!context || context.source != source || event.t_ms > context.expiresAt)
				continue;
			// A synchronous effect can fire before OnEntityCreatedServer finishes.
			if (context.createdAt - event.t_ms > 2500)
				continue;
			event.authority = context;
			return;
		}
	}
}
