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

export interface SeededE2EUser {
  label: string;
  user_id: string;
  email: string;
  password: string;
}

export interface SeededCollectionManagementFixture {
  owner: SeededE2EUser;
  member: SeededE2EUser;
  collection: {
    id: string;
    title: string;
    description: string;
    visibility: 'private' | 'unlisted';
  };
  invite: {
    id: string;
    token: string;
    join_path: string;
  };
  saved_memes: SeededMeme[];
  pinned_memes: SeededMeme[];
}

export interface SeedArtifact {
  run_id: string;
  seeded_memes: SeededMeme[];
  collection_management: SeededCollectionManagementFixture;
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

export function collectionManagementFixture(seed: SeedArtifact): SeededCollectionManagementFixture {
  if (!seed.collection_management) throw new Error('Seed artifact did not include collection_management.');
  return seed.collection_management;
}
