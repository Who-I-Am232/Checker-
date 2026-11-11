modded class SCR_BaseGameMode
{
	override void OnGameStart()
	{
		super.OnGameStart();
		CA_Runtime.Start();
	}

	override void OnGameEnd()
	{
		CA_Runtime.Stop();
		super.OnGameEnd();
	}

	override void OnPlayerAuditSuccess(int playerId)
	{
		super.OnPlayerAuditSuccess(playerId);
		if (Replication.IsServer() && CA_Runtime.Get())
			CA_Runtime.Get().RegisterIdentity(playerId);
	}

	override void OnPlayerDisconnected(int playerId, KickCauseCode cause, int timeout)
	{
		if (Replication.IsServer() && CA_Runtime.Get())
			CA_Runtime.Get().ForgetIdentity(playerId);
		super.OnPlayerDisconnected(playerId, cause, timeout);
	}
}

modded class SCR_EditableEntityComponent
{
	override void OnPostInit(IEntity owner)
	{
		super.OnPostInit(owner);
		if (Replication.IsServer() && CA_Runtime.Get())
			CA_Runtime.Get().ObserveEntity(owner);
	}
}

modded class SCR_GameModeCampaign
{
	override void OnEntityRequested(notnull IEntity spawnedEntity, IEntity user, SCR_Faction faction, SCR_MilitaryBaseLogicComponent service)
	{
		super.OnEntityRequested(spawnedEntity, user, faction, service);
		if (Replication.IsServer() && CA_Runtime.Get())
		{
			int actor = GetGame().GetPlayerManager().GetPlayerIdFromControlledEntity(user);
			CA_Runtime.Get().ObserveAction(spawnedEntity, actor, "conflict_request");
		}
	}
}

modded class SCR_PlacingEditorComponent
{
	override protected void OnEntityCreatedServer(array<SCR_EditableEntityComponent> entities)
	{
		super.OnEntityCreatedServer(entities);
		if (!Replication.IsServer() || !CA_Runtime.Get() || !GetManager())
			return;
		// Read the server's manager, not an arbitrary playerID from an RPC payload.
		int actor = GetManager().GetPlayerID();
		foreach (SCR_EditableEntityComponent editable : entities)
		{
			if (editable)
				CA_Runtime.Get().ObserveAction(editable.GetOwner(), actor, "editor_place");
		}
	}
}

modded class SCR_DamageManagerComponent
{
	override protected void OnDamage(notnull BaseDamageContext damageContext)
	{
		// Capture before another listener can mutate the shared context.
		if (Replication.IsServer() && CA_Runtime.Get())
			CA_Runtime.Get().ObserveDamage(GetOwner(), damageContext);
		super.OnDamage(damageContext);
	}
}

modded class SCR_ExplosiveTriggerComponent
{
	override void UseTrigger()
	{
		RplComponent rpl = RplComponent.Cast(GetOwner().FindComponent(RplComponent));
		if (Replication.IsServer() && rpl && !rpl.IsProxy() && CA_Runtime.Get())
			CA_Runtime.Get().ObserveTrigger(GetOwner(), GetInstigator());
		super.UseTrigger();
	}
}
