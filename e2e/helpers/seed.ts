import { readFileSync } from 'node:fs';
import path from 'node:path';

export interface SeededMeme {
  category: string;
  meme_id: string;
  meme_file_id: string;
  slug: string;
  query: string;
  object_key: string;
  title: string;
  tags: string[];
  is_nsfw: boolean;
  language: string;
  media_type: string;
}

export interface SeedArtifact {
  run_id: string;
  seeded_memes: SeededMeme[];
  created_meme: {
    meme_id: string;
    meme_file_id: string;
    slug: string;
    query: string;
    title: string;
  };
  proof?: {
    dual_index?: {
      both_targets_searchable?: boolean;
    };
  };
}

export function readSeedArtifact(): SeedArtifact {
  const artifactsDir = process.env.E2E_ARTIFACTS_DIR ?? '/artifacts';
  const seedPath = process.env.E2E_SEED_FILE ?? path.join(artifactsDir, 'seed.json');
  return JSON.parse(readFileSync(seedPath, 'utf8')) as SeedArtifact;
}

export function seededByCategory(seed: SeedArtifact, category: string): SeededMeme {
  const seeded = seed.seeded_memes.find((item) => item.category === category);
  if (!seeded) throw new Error(`Seed artifact did not include ${category}.`);
  return seeded;
}
