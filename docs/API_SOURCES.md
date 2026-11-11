# API verification and provenance

Inspected on 2026-11-11. Engine compiler/runtime validation is **not** implied.
The primary reference is Bohemia Interactive's source snapshot **1.9.0.37**,
[commit 4317af710d2f748626e7a5e205e0990546a799e5](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/commit/4317af710d2f748626e7a5e205e0990546a799e5),
dated 2026-11-05. The exact target server version has not been supplied.
Online generated API documentation can lag behind this snapshot; the source
snapshot was used to check hook signatures and behavior.

| Integration | Primary source |
|---|---|
| Runtime start/end and audited identities | [SCR_BaseGameMode.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/GameMode/SCR_BaseGameMode.c) |
| Editable OnPostInit, parent, type; author means last modifier | [SCR_EditableEntityComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Editor/Components/EditableEntity/SCR_EditableEntityComponent.c) |
| Successful placement callback and server manager ownership | [SCR_PlacingEditorComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Editor/Components/Editor/SCR_PlacingEditorComponent.c) |
| Conflict's OnEntityRequested | [SCR_GameModeCampaign.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/GameMode/SCR_GameModeCampaign.c) |
| Building provider path | [SCR_CampaignBuildingManagerComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Campaign/SCR_CampaignBuildingManagerComponent.c) |
| Damage notification, super chain | [SCR_DamageManagerComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/GameCode/Components/SCR_DamageManagerComponent.c) |
| Damage fields and instigator | [BaseDamageContext.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/generated/Damage/BaseDamageContext.c) |
| EXPLOSIVE enum | [EDamageType.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/generated/HitZone/EDamageType.c) |
| Charge trigger and instigator access | [SCR_ExplosiveTriggerComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Weapon/Explosives/SCR_ExplosiveTriggerComponent.c), [SCR_BaseTriggerComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Weapon/Explosives/SCR_BaseTriggerComponent.c) |
| Spatial query callback cancellation | [BaseWorld.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Core/generated/World/BaseWorld.c) |
| Connected players and controlled entity | [PlayerManager.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/generated/Network/PlayerManager.c) |
| Identity lookup only after successful audit | [SCR_PlayerIdentityUtils.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/Utilities/SCR_PlayerIdentityUtils.c) |
| JSON serialization preset and real WriteValue usage | [JsonSaveContext.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/generated/Plugins/Serialization/Presets/JsonSaveContext.c), [SCR_ArsenalManagerComponent.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Game/GameMode/Loadout/SCR_ArsenalManagerComponent.c) |
| File writing, byte counts and directories | [FileHandle.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Core/generated/System/FileHandle.c), [FileIO.c](https://github.com/BohemiaInteractive/Arma-Reforger-Script-Diff/blob/4317af710d2f748626e7a5e205e0990546a799e5/scripts/Core/generated/System/FileIO.c) |
| Project syntax and base-game dependency GUID | [Official sample gproj](https://github.com/BohemiaInteractive/Arma-Reforger-Samples/blob/83f12390d3dd61ed834d379f621520ed9f1b891d/SampleMod_Main/SampleMod.gproj) |

Signatures used by the adapter:

```text
SCR_BaseGameMode.OnGameStart()
SCR_BaseGameMode.OnGameEnd()
SCR_BaseGameMode.OnPlayerAuditSuccess(int playerId)
SCR_BaseGameMode.OnPlayerDisconnected(int playerId, KickCauseCode cause, int timeout)
SCR_EditableEntityComponent.OnPostInit(IEntity owner)
SCR_GameModeCampaign.OnEntityRequested(notnull IEntity spawnedEntity, IEntity user,
    SCR_Faction faction, SCR_MilitaryBaseLogicComponent service)
SCR_PlacingEditorComponent.OnEntityCreatedServer(array<SCR_EditableEntityComponent> entities)
SCR_DamageManagerComponent.OnDamage(notnull BaseDamageContext damageContext)
SCR_ExplosiveTriggerComponent.UseTrigger()
```

All adapter overrides preserve their `super` call. No global native spawn or
explosion callback was assumed. A separate bounded query is used for additional
world observations, with explicit incomplete/baseline states.

Additional official references:

- [Server startup parameters, including logStats](https://community.bistudio.com/wiki/Arma_Reforger:Startup_Parameters)
- [Scripting performance guidance](https://community.bistudio.com/wiki/Arma_Reforger:Scripting_Performance)
- [Script profiling](https://community.bistudio.com/wiki/Arma_Reforger:Script_Profiling)

Movement sampler references inspected for this extension:

- [CompartmentAccessComponent](https://community.bistudio.com/wikidata/external-data/arma-reforger/ArmaReforgerScriptAPIPublic/interfaceCompartmentAccessComponent.html):
  `GetCompartment()`, `IsGettingIn()`, `IsGettingOut()`, `IsSwitchingSeatsAnim()`.
- [PilotCompartmentSlot](https://community.bistudio.com/wikidata/external-data/arma-reforger/ArmaReforgerScriptAPIPublic/interfacePilotCompartmentSlot.html):
  driving/pilot slot; inherited `GetVehicle()`. The base game mode above also
  uses a PilotCompartmentSlot cast when handling a player's vehicle.
- [Physics](https://community.bistudio.com/wikidata/external-data/arma-reforger/EnfusionScriptAPIPublic/interfacePhysics.html):
  `GetVelocity()` returns the linear velocity vector.

Discord integration follows the official API documentation:

- [Execute Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook):
  content limit, `allowed_mentions`, and `wait=true` for a returned message.
- [Rate limits](https://docs.discord.com/developers/topics/rate-limits):
  429 `retry_after` and rate-limit response headers. Limits are not assumed
  constant; response instructions govern retry delays.

HTTP behavior is exercised with a fake transport and a mocked HTTP opener.
No Discord credential or live delivery was used during validation.

The source files in this repository are new application code. Third-party game
source files, binaries, credentials and actual player telemetry are not bundled.
