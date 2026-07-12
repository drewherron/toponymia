export interface FeatureCandidate {
  name: string
  kind: string
  sourceLayer: string
  properties: Record<string, unknown>
}
