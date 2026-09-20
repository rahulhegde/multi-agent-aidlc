"""Project identity and dependency invalidation for current output snapshots."""

from aidlc.domain.models import ArtifactKind

# Decisions and measured execution retain individual identities; reasoning outputs are snapshots.
SNAPSHOT_KINDS = {
    ArtifactKind.PROJECT_BRIEF,
    ArtifactKind.REQUIREMENTS_SPEC,
    ArtifactKind.ARCHITECTURE_DECISION,
    ArtifactKind.UX_SPECIFICATION,
    ArtifactKind.THREAT_MODEL,
    ArtifactKind.TEST_PLAN,
    ArtifactKind.IMPLEMENTATION_PLAN,
    ArtifactKind.CODE_CHANGE,
    ArtifactKind.INTEGRATED_SOURCE,
    ArtifactKind.BUILD_REPORT,
    ArtifactKind.TEST_REPORT,
    ArtifactKind.STATIC_ANALYSIS_REPORT,
    ArtifactKind.QUALITY_GATE_REPORT,
    ArtifactKind.EVALUATION_REPORT,
    ArtifactKind.RELEASE_BUNDLE,
}

_GROUPS = [
    {ArtifactKind.PROJECT_BRIEF},
    {ArtifactKind.REQUIREMENTS_SPEC},
    {
        ArtifactKind.ARCHITECTURE_DECISION,
        ArtifactKind.UX_SPECIFICATION,
        ArtifactKind.THREAT_MODEL,
        ArtifactKind.TEST_PLAN,
    },
    {ArtifactKind.IMPLEMENTATION_PLAN},
    {ArtifactKind.CODE_CHANGE},
    {ArtifactKind.INTEGRATED_SOURCE},
    {ArtifactKind.BUILD_REPORT, ArtifactKind.TEST_REPORT, ArtifactKind.STATIC_ANALYSIS_REPORT},
    {ArtifactKind.QUALITY_GATE_REPORT},
    {ArtifactKind.EVALUATION_REPORT},
    {ArtifactKind.RELEASE_BUNDLE},
]


def invalidated_kinds(kind: ArtifactKind) -> set[ArtifactKind]:
    for index, group in enumerate(_GROUPS):
        if kind in group:
            return set().union(*_GROUPS[index + 1 :])
    return set()
