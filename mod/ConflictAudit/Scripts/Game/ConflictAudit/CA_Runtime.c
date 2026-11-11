class CA_Runtime
{
	protected static ref CA_Runtime s_Instance;
	protected ref array<ref CA_Event> m_Queue = {};
	protected ref array<ref CA_Cell> m_Cells = {};
	protected ref map<int, string> m_Identity = new map<int, string>();
	protected ref set<string> m_QueryIDs;
	protected ref array<IEntity> m_QueryEntities;
	protected int m_QueryCount;
	protected int m_QueryStart;
	protected int m_PlayerCursor;
	protected int m_StartTick;
	protected int m_Sequence;
	protected int m_Dropped;
	protected int m_FilePart;
	protected int m_FileBytes;
	protected int m_LastHeartbeat;
	protected bool m_StorageStopped;
	protected string m_Session;
	protected string m_Path;
	protected ref CA_MovementSampler m_Movement;
	protected ref CA_AuthorityTracker m_Authority;

	static CA_Runtime Get()
	{
		return s_Instance;
	}

	static void Start()
	{
		if (!GetGame() || !GetGame().InPlayMode() || !Replication.IsServer())
			return;
		Stop();
		s_Instance = new CA_Runtime();
		s_Instance.Begin();
	}

	static void Stop()
	{
		if (!s_Instance)
			return;
		GetGame().GetCallqueue().Remove(s_Instance.Tick);
		s_Instance.Enqueue(s_Instance.NewEvent("session_end", "runtime"));
		// Rotation can end a flush early. Drain within the finite disk budget.
		for (int i = 0; i < CA_Settings.MAX_FILES_PER_SESSION + 1; i++)
		{
			if (s_Instance.m_Queue.IsEmpty() || s_Instance.m_StorageStopped)
				break;
			s_Instance.Flush(CA_Settings.MAX_QUEUE + 1);
		}
		s_Instance = null;
	}

	protected void Begin()
	{
		m_StartTick = System.GetTickCount();
		m_Movement = new CA_MovementSampler();
		m_Authority = new CA_AuthorityTracker();
		m_Session = string.Format("%1-%2-%3", System.GetUnixTime(), m_StartTick, Math.RandomInt(1, 1000000000));
		FileIO.MakeDirectory("$profile:ConflictAudit");
		SelectFile();
		CA_Event event = NewEvent("session_start", "runtime");
		event.category = "prototype_api_1.9.0.37";
		Enqueue(event);
		GetGame().GetCallqueue().CallLater(Tick, CA_Settings.TICK_MS, true);
		Print("[ConflictAudit] Observation started: " + m_Path, LogLevel.NORMAL);
	}

	int Now()
	{
		return System.GetTickCount() - m_StartTick;
	}

	CA_Event NewEvent(string kind, string source)
	{
		CA_Event event = new CA_Event();
		event.kind = kind;
		event.source = source;
		event.t_ms = Now();
		event.utc = System.GetUnixTime();
		event.warmup = event.t_ms < CA_Settings.STARTUP_GRACE_MS;
		return event;
	}

	static string EntityKey(IEntity entity)
	{
		if (!entity)
			return "";
		return string.Format("%1", entity.GetID());
	}

	static string Prefab(IEntity entity)
	{
		if (!entity || !entity.GetPrefabData())
			return "";
		return entity.GetPrefabData().GetPrefabName();
	}

	static string Category(IEntity entity)
	{
		SCR_EditableEntityComponent editable = SCR_EditableEntityComponent.Cast(entity.FindComponent(SCR_EditableEntityComponent));
		if (editable)
			return typename.EnumToString(EEditableEntityType, editable.GetEntityType());
		return "UNCLASSIFIED";
	}

	protected void Snapshot(CA_Event event, IEntity entity)
	{
		if (!entity)
			return;
		event.entity = EntityKey(entity);
		event.prefab = Prefab(entity);
		event.category = Category(entity);
		event.position = entity.GetOrigin();
		event.parent = EntityKey(entity.GetParent());
		SCR_EditableEntityComponent editable = SCR_EditableEntityComponent.Cast(entity.FindComponent(SCR_EditableEntityComponent));
		if (editable && editable.GetParentEntity())
			event.parent = EntityKey(editable.GetParentEntity().GetOwner());
	}

	void RegisterIdentity(int playerID)
	{
		// Called ONLY after successful backend audit, never from a client payload.
		string uid = SCR_PlayerIdentityUtils.GetPlayerIdentityId(playerID);
		m_Identity.Set(playerID, uid);
		CA_Event event = NewEvent("player_identity", "backend_audit");
		SetActor(event, playerID, "identity_only");
		Enqueue(event);
	}

	void ForgetIdentity(int playerID)
	{
		CA_Event event = NewEvent("player_left", "server_callback");
		SetActor(event, playerID, "identity_only");
		Enqueue(event);
		m_Identity.Remove(playerID);
		if (m_Movement)
			m_Movement.Forget(playerID);
	}

	string GetAuditedIdentity(int playerID)
	{
		return m_Identity.Get(playerID);
	}

	// Call only from a server-side, permission-validated legitimate teleport path.
	// This method is deliberately not exposed as an RPC or a player command.
	void AllowMovementReset(int playerID, int durationMs, string reason)
	{
		if (!Replication.IsServer() || !m_Movement || playerID <= 0)
			return;
		m_Movement.Suppress(playerID, Now() + Math.Clamp(durationMs, 1000, 60000));
		CA_Event event = NewEvent("movement_reset", "authorized_server_adapter");
		event.category = reason;
		SetActor(event, playerID, "server_action_callback");
		Enqueue(event);
	}

	void ObserveMovement(int playerID, IEntity controlled, IEntity tracked, vector previous, int elapsed, int sampleTime, string mode)
	{
		CA_Event event = NewEvent("movement_candidate", "movement_sampler");
		Snapshot(event, tracked);
		event.source_entity = EntityKey(controlled);
		event.t_ms = sampleTime;
		event.from_position = previous;
		event.delta_ms = elapsed;
		event.motion_mode = mode;
		event.distance_m = vector.Distance(previous, event.position);
		event.avg_speed_mps = event.distance_m * 1000 / elapsed;
		Physics physics = tracked.GetPhysics();
		if (physics)
		{
			event.inst_speed_valid = true;
			event.inst_speed_mps = vector.Distance(vector.Zero, physics.GetVelocity());
		}
		SetActor(event, playerID, "server_movement_sample");
		// Keep the measured position; never replace it with a later flush position.
		Enqueue(event);
	}

	protected void SetActor(CA_Event event, int playerID, string attribution)
	{
		if (playerID <= 0)
			return;
		event.actor_id = playerID;
		// An old projectile may retain an ID after disconnect. Do not attach the
		// current occupant's UID/name to an engine instigator's possibly reused ID.
		if (attribution != "engine_instigator")
		{
			event.actor_uid = m_Identity.Get(playerID);
			event.actor_name = GetGame().GetPlayerManager().GetPlayerName(playerID);
			event.actor_role = CA_AuthorityTracker.Role(playerID);
		}
		event.attribution = attribution;
	}

	void ObserveEntity(IEntity entity, string kind = "entity_init")
	{
		if (!entity || entity.GetWorld() != GetGame().GetWorld() || m_StorageStopped)
			return;
		if (m_Queue.Count() >= CA_Settings.MAX_QUEUE)
		{
			m_Dropped++;
			return;
		}
		CA_Event event = NewEvent(kind, "editable_component");
		Snapshot(event, entity);
		event.deferredEntity = entity;
		Enqueue(event);
	}

	void ObserveGMPlacement(IEntity entity, SCR_EditorManagerEntity manager, SCR_EditorModeEntity mode)
	{
		if (!Replication.IsServer() || !entity || !manager || m_StorageStopped)
			return;
		CA_GmSource context = m_Authority.Register(this, entity, manager, mode);
		ObserveAction(entity, manager.GetPlayerID(), "editor_place", context);
	}

	// Invoke only inside the server FX generator after it creates this projectile.
	bool RegisterGMEffectSource(IEntity projectile, IEntity authorizedGenerator)
	{
		if (!Replication.IsServer() || !m_Authority || m_StorageStopped)
			return false;
		return m_Authority.Link(this, projectile, authorizedGenerator);
	}

	void ObserveAction(IEntity entity, int playerID, string route, CA_GmSource authority = null)
	{
		if (!entity || entity.GetWorld() != GetGame().GetWorld() || m_StorageStopped)
			return;
		if (m_Queue.Count() >= CA_Settings.MAX_QUEUE)
		{
			m_Dropped++;
			return;
		}
		CA_Event event = NewEvent("placement", route);
		Snapshot(event, entity);
		event.deferredEntity = entity;
		SetActor(event, playerID, "server_action_callback");
		event.authority = authority;
		Enqueue(event);
	}

	void ObserveDamage(IEntity owner, BaseDamageContext damage)
	{
		if (damage.damageType != EDamageType.EXPLOSIVE || !owner || m_StorageStopped)
			return;
		if (owner.GetWorld() != GetGame().GetWorld())
			return;
		if (m_Queue.Count() >= CA_Settings.MAX_QUEUE)
		{
			m_Dropped++;
			return;
		}
		CA_Event event = NewEvent("explosive_damage", "damage_manager");
		Snapshot(event, owner);
		event.position = damage.hitPosition;
		event.amount = damage.damageValue;
		event.source_entity = EntityKey(damage.damageSource);
		if (damage.instigator)
			SetActor(event, damage.instigator.GetInstigatorPlayerID(), "engine_instigator");
		m_Authority.Capture(event, damage.damageSource);
		Enqueue(event);
	}

	void ObserveTrigger(IEntity entity, Instigator instigator)
	{
		if (!entity || m_StorageStopped || entity.GetWorld() != GetGame().GetWorld())
			return;
		if (m_Queue.Count() >= CA_Settings.MAX_QUEUE)
		{
			m_Dropped++;
			return;
		}
		CA_Event event = NewEvent("explosive_trigger", "explosive_trigger_component");
		Snapshot(event, entity);
		if (instigator)
			SetActor(event, instigator.GetInstigatorPlayerID(), "engine_instigator");
		m_Authority.Capture(event, entity);
		Enqueue(event);
	}

	void Enqueue(CA_Event event)
	{
		if (m_StorageStopped)
			return;
		if (m_Queue.Count() >= CA_Settings.MAX_QUEUE)
		{
			m_Dropped++;
			return;
		}
		m_Queue.Insert(event);
	}

	protected void Tick()
	{
		if (m_StorageStopped || !Replication.IsServer())
			return;
		// Drain before scanning to keep telemetry lag bounded during a spawn burst.
		Flush(CA_Settings.MAX_EVENTS_PER_TICK);
		m_Authority.Sweep(Now());
		if (CA_Settings.MOVEMENT_ENABLED && m_Movement && !m_StorageStopped)
			m_Movement.Sample(this);
		if (CA_Settings.SCANNER_ENABLED && m_Queue.Count() < CA_Settings.MAX_QUEUE / 2)
			ScanOneCell();
		if (Now() - m_LastHeartbeat >= 10000)
		{
			CA_Event heartbeat = NewEvent("heartbeat", "runtime");
			heartbeat.count = m_Queue.Count();
			Enqueue(heartbeat);
			m_LastHeartbeat = Now();
		}
	}

	protected void SelectFile()
	{
		m_Path = string.Format("$profile:ConflictAudit/events-%1-%2.jsonl", m_Session, m_FilePart);
		m_FileBytes = 0;
	}

	protected void Flush(int limit)
	{
		if (m_StorageStopped)
			return;
		if (m_Queue.IsEmpty() && m_Dropped == 0)
			return;
		FileHandle file = FileIO.OpenFile(m_Path, FileMode.APPEND);
		if (!file)
		{
			StopStorage("cannot open audit file");
			return;
		}
		if (m_Dropped > 0)
		{
			CA_Event loss = NewEvent("telemetry_loss", "queue_limit");
			loss.count = m_Dropped;
			// Preserve event-time order: a loss report must not overtake older events.
			m_Queue.Insert(loss);
			m_Dropped = 0;
		}
		int count = Math.Min(limit, m_Queue.Count());
		for (int i = 0; i < count; i++)
		{
			CA_Event event = m_Queue[0];
			m_Queue.Remove(0);
			if (event.deferredEntity)
				Snapshot(event, event.deferredEntity);
			event.deferredEntity = null;
			m_Authority.Resolve(event);
			event.permissionCandidates = null;
			event.seq = ++m_Sequence;
			string line = event.Serialize(m_Session) + "\n";
			int written = file.Write(line);
			if (written != line.Length())
			{
				file.Close();
				StopStorage("incomplete audit write");
				return;
			}
			m_FileBytes += written;
			if (m_FileBytes >= CA_Settings.FILE_BYTES)
			{
				file.Close();
				m_FilePart++;
				if (m_FilePart >= CA_Settings.MAX_FILES_PER_SESSION)
				{
					StopStorage("per-session disk budget reached");
					return;
				}
				SelectFile();
				// Remaining observations stay queued for the next bounded flush.
				return;
			}
		}
		file.Close();
	}

	protected void StopStorage(string reason)
	{
		m_StorageStopped = true;
		m_Queue.Clear();
		Print("[ConflictAudit] AUDIT STOPPED: " + reason + ". No further evidence is being collected.", LogLevel.ERROR);
	}

	protected void ScanOneCell()
	{
		array<int> players = {};
		GetGame().GetPlayerManager().GetPlayers(players);
		if (players.IsEmpty())
			return;
		if (m_PlayerCursor >= players.Count())
			m_PlayerCursor = 0;
		IEntity player = GetGame().GetPlayerManager().GetPlayerControlledEntity(players[m_PlayerCursor++]);
		if (!player)
			return;
		vector pos = player.GetOrigin();
		int cx = Math.Floor(pos[0] / CA_Settings.CELL_METERS);
		int cz = Math.Floor(pos[2] / CA_Settings.CELL_METERS);
		string key = string.Format("%1:%2", cx, cz);
		CA_Cell previous;
		foreach (CA_Cell candidate : m_Cells)
		{
			if (candidate.key == key)
			{
				previous = candidate;
				break;
			}
		}
		// Avoid rescanning a crowded base once per player.
		if (previous && Now() - previous.lastScan < 5000)
			return;
		m_QueryCount = 0;
		m_QueryStart = System.GetTickCount();
		m_QueryIDs = new set<string>();
		m_QueryEntities = {};
		vector mins = Vector(cx * CA_Settings.CELL_METERS, -1000, cz * CA_Settings.CELL_METERS);
		vector maxs = mins + Vector(CA_Settings.CELL_METERS, 10000, CA_Settings.CELL_METERS);
		bool complete = GetGame().GetWorld().QueryEntitiesByAABB(mins, maxs, QueryEntity);
		if (!complete)
		{
			CA_Event truncated = NewEvent("scan_incomplete", "spatial_scan");
			truncated.count = m_QueryCount;
			truncated.position = pos;
			Enqueue(truncated);
			// An incomplete snapshot must NEVER become a baseline or produce diffs.
			if (previous)
				m_Cells.RemoveItem(previous);
			ClearQuery();
			return;
		}
		bool baseline = !previous;
		if (previous && Now() - previous.lastScan > CA_Settings.REBASE_AFTER_MS)
			baseline = true;
		if (!baseline && Now() >= CA_Settings.STARTUP_GRACE_MS)
		{
			foreach (IEntity entity : m_QueryEntities)
			{
				if (!entity || previous.ids.Contains(EntityKey(entity)))
					continue;
				CA_Event added = NewEvent("scan_new", "spatial_scan");
				Snapshot(added, entity);
				// A spatial observation has no author. No nearest-player attribution.
				Enqueue(added);
			}
		}
		if (!previous)
		{
			if (m_Cells.Count() >= CA_Settings.MAX_CELLS)
			{
				int oldestIndex;
				for (int j = 1; j < m_Cells.Count(); j++)
				{
					if (m_Cells[j].lastScan < m_Cells[oldestIndex].lastScan)
					oldestIndex = j;
				}
				m_Cells.Remove(oldestIndex);
			}
			previous = new CA_Cell();
			previous.key = key;
			m_Cells.Insert(previous);
		}
		previous.ids = m_QueryIDs;
		previous.lastScan = Now();
		if (baseline)
		{
			CA_Event baseEvent = NewEvent("scan_baseline", "spatial_scan");
			baseEvent.count = m_QueryIDs.Count();
			baseEvent.position = pos;
			Enqueue(baseEvent);
		}
		ClearQuery();
	}

	protected bool QueryEntity(IEntity entity)
	{
		m_QueryCount++;
		if (m_QueryCount > CA_Settings.MAX_QUERY_ENTITIES || System.GetTickCount() - m_QueryStart >= CA_Settings.QUERY_BUDGET_MS)
			return false;
		if (!entity || entity.GetParent() || Prefab(entity).IsEmpty())
			return true;
		string category = Category(entity);
		// Do not treat ordinary character/vehicle motion as construction.
		if (category != "GENERIC" && category != "UNCLASSIFIED")
			return true;
		if (ChimeraCharacter.Cast(entity) || BaseVehicle.Cast(entity))
			return true;
		string id = EntityKey(entity);
		if (!m_QueryIDs.Contains(id))
		{
			m_QueryIDs.Insert(id);
			m_QueryEntities.Insert(entity);
		}
		return true;
	}

	protected void ClearQuery()
	{
		m_QueryEntities = null;
		m_QueryIDs = null;
	}
}
