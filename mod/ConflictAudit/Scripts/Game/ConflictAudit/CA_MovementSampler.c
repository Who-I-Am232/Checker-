class CA_MotionState
{
	IEntity controlled;
	IEntity tracked;
	string uid;
	string mode;
	vector position;
	int sampledAt;
	int stableSince;
	int suppressedUntil;
}

// Samples current authoritative world positions, not client-reported telemetry.
// Only candidates are logged to avoid writing every player's location each second.
class CA_MovementSampler
{
	protected ref map<int, ref CA_MotionState> m_States = new map<int, ref CA_MotionState>();
	protected int m_LastCoverage;

	void Forget(int playerID)
	{
		m_States.Remove(playerID);
	}

	void Suppress(int playerID, int until)
	{
		CA_MotionState state = m_States.Get(playerID);
		if (!state)
		{
			if (m_States.Count() >= CA_Settings.MAX_MOVEMENT_PLAYERS)
				return;
			state = new CA_MotionState();
			m_States.Set(playerID, state);
		}
		state.suppressedUntil = until;
	}

	void Sample(CA_Runtime runtime)
	{
		array<int> players = {};
		PlayerManager manager = GetGame().GetPlayerManager();
		manager.GetPlayers(players);
		int limit = Math.Min(players.Count(), CA_Settings.MAX_MOVEMENT_PLAYERS);
		if (players.Count() > limit && runtime.Now() - m_LastCoverage >= 10000)
		{
			CA_Event loss = runtime.NewEvent("telemetry_loss", "movement_player_limit");
			loss.count = players.Count() - limit;
			runtime.Enqueue(loss);
			m_LastCoverage = runtime.Now();
		}
		for (int index = 0; index < limit; index++)
		{
			int playerID = players[index];
			IEntity controlled = manager.GetPlayerControlledEntity(playerID);
			if (!controlled || !ChimeraCharacter.Cast(controlled))
			{
				Forget(playerID);
				continue;
			}
			IEntity tracked = controlled;
			string mode = "FOOT";
			CompartmentAccessComponent access = CompartmentAccessComponent.Cast(controlled.FindComponent(CompartmentAccessComponent));
			if (access && (access.IsGettingIn() || access.IsGettingOut() || access.IsSwitchingSeatsAnim()))
			{
				Forget(playerID);
				continue;
			}
			if (access && access.GetCompartment())
			{
				PilotCompartmentSlot pilot = PilotCompartmentSlot.Cast(access.GetCompartment());
				// Passengers must not be blamed for the vehicle's movement.
				if (!pilot || !pilot.GetVehicle())
				{
					Forget(playerID);
					continue;
				}
				tracked = pilot.GetVehicle();
				mode = "VEHICLE";
			}
			int now = runtime.Now();
			string uid = runtime.GetAuditedIdentity(playerID);
			CA_MotionState state = m_States.Get(playerID);
			if (!state)
			{
				if (m_States.Count() >= CA_Settings.MAX_MOVEMENT_PLAYERS)
					continue;
				state = new CA_MotionState();
				m_States.Set(playerID, state);
			}
			int elapsed = now - state.sampledAt;
			bool reset = state.controlled != controlled || state.tracked != tracked || state.uid != uid || state.mode != mode;
			if (elapsed < 250 || elapsed > CA_Settings.MOVEMENT_MAX_GAP_MS || now < state.suppressedUntil)
				reset = true;
			vector pos = tracked.GetOrigin();
			if (reset)
				state.stableSince = now;
			else if (now >= CA_Settings.STARTUP_GRACE_MS && now - state.stableSince >= CA_Settings.MOVEMENT_GRACE_MS)
			{
				float distance = vector.Distance(state.position, pos);
				float speed = distance * 1000 / elapsed;
				// Collector floors, intentionally below analyzer defaults. Changing
				// analyzer thresholds below these floors cannot recover unlogged samples.
				bool candidate = distance >= 30 && speed >= 20;
				if (mode == "VEHICLE")
					candidate = distance >= 100 && speed >= 100;
				if (candidate)
					runtime.ObserveMovement(playerID, controlled, tracked, state.position, elapsed, now, mode);
			}
			state.controlled = controlled;
			state.tracked = tracked;
			state.uid = uid;
			state.mode = mode;
			state.position = pos;
			state.sampledAt = now;
		}
	}
}
