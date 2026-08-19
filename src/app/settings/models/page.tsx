'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';

type Model = {
  id: string;
  name: string;
};

type Provider = {
  id: string;
  name: string;
  supportsCustomModel: boolean;
  models: Model[];
};

type ModelConfig = {
  providers: Provider[];
  defaultProvider: string;
};

type ModelRole = 'generator' | 'reviewer' | 'fast';

type RoleSelection = {
  provider: string;
  model: string;
};

type ModelProfile = Record<ModelRole, RoleSelection> & {
  embeddingProvider: 'openai' | 'google' | 'ollama' | 'bedrock';
  embeddingModel: string;
  maxConcurrency: number;
  nightlyBudget: number;
};

const STORAGE_KEY = 'codeinsight.modelProfile.v1';

const ROLE_META: Record<ModelRole, { title: string; description: string }> = {
  generator: {
    title: '分析与生成模型',
    description: '负责功能摘要、架构理解、业务流程和文档生成。',
  },
  reviewer: {
    title: '独立复核模型',
    description: '检查证据、矛盾和过度推断，建议与生成模型使用不同模型。',
  },
  fast: {
    title: '快速任务模型',
    description: '负责分类、路由、变更判断等高频低成本任务。',
  },
};

const EMBEDDING_DEFAULTS: Record<ModelProfile['embeddingProvider'], string> = {
  openai: 'text-embedding-3-small',
  google: 'gemini-embedding-001',
  ollama: 'nomic-embed-text',
  bedrock: 'amazon.titan-embed-text-v2:0',
};

function createDefaultProfile(config: ModelConfig): ModelProfile {
  const provider =
    config.providers.find((item) => item.id === config.defaultProvider) ??
    config.providers[0];
  const model = provider?.models[0]?.id ?? '';
  const selection = { provider: provider?.id ?? '', model };

  return {
    generator: selection,
    reviewer: selection,
    fast: selection,
    embeddingProvider: 'openai',
    embeddingModel: EMBEDDING_DEFAULTS.openai,
    maxConcurrency: 2,
    nightlyBudget: 0,
  };
}

export default function ModelSettingsPage() {
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [profile, setProfile] = useState<ModelProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    const load = async () => {
      try {
        const response = await fetch('/api/models/config');
        if (!response.ok) {
          throw new Error(`模型配置接口返回 ${response.status}`);
        }

        const nextConfig = (await response.json()) as ModelConfig;
        setConfig(nextConfig);

        const stored = localStorage.getItem(STORAGE_KEY);
        setProfile(stored ? JSON.parse(stored) : createDefaultProfile(nextConfig));
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : '无法加载模型配置');
      } finally {
        setLoading(false);
      }
    };

    load();
  }, []);

  const providerMap = useMemo(
    () => new Map(config?.providers.map((provider) => [provider.id, provider]) ?? []),
    [config],
  );

  const updateRole = (
    role: ModelRole,
    field: keyof RoleSelection,
    value: string,
  ) => {
    setProfile((current) => {
      if (!current) return current;

      if (field === 'provider') {
        const nextProvider = providerMap.get(value);
        return {
          ...current,
          [role]: {
            provider: value,
            model: nextProvider?.models[0]?.id ?? '',
          },
        };
      }

      return {
        ...current,
        [role]: {
          ...current[role],
          model: value,
        },
      };
    });
  };

  const save = () => {
    if (!profile) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(profile));
    setStatus('模型职责配置已保存到本机。后续桌面端将迁移到系统安全存储。');
  };

  const checkService = async () => {
    setStatus('正在检查本地分析服务...');
    try {
      const response = await fetch('/api/models/config', { cache: 'no-store' });
      if (!response.ok) throw new Error(String(response.status));
      setStatus('本地分析服务连接正常。Provider 密钥验证将在安全配置阶段接入。');
    } catch {
      setStatus('本地分析服务不可用，请确认后台 daemon 已启动。');
    }
  };

  if (loading) {
    return <main className="min-h-screen p-8">正在加载模型配置...</main>;
  }

  if (error || !config || !profile) {
    return (
      <main className="min-h-screen p-8">
        <div className="mx-auto max-w-3xl rounded-xl border border-red-500/30 p-6">
          <h1 className="text-xl font-semibold">模型配置加载失败</h1>
          <p className="mt-2 text-sm text-[var(--muted)]">{error}</p>
          <Link className="mt-4 inline-block text-[var(--accent-primary)]" href="/">
            返回项目首页
          </Link>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[var(--background)] px-5 py-8 text-[var(--foreground)]">
      <div className="mx-auto max-w-6xl">
        <header className="flex flex-col gap-4 border-b border-[var(--border-color)] pb-6 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="text-sm font-medium text-[var(--accent-primary)]">CodeInsight-AI</div>
            <h1 className="mt-1 text-3xl font-semibold">模型与分析策略</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-[var(--muted)]">
              将生成、复核、快速任务和向量检索分开配置，避免单一模型同时生成和自我验证。
            </p>
          </div>
          <Link
            href="/"
            className="rounded-lg border border-[var(--border-color)] px-4 py-2 text-sm hover:bg-[var(--card-bg)]"
          >
            返回项目
          </Link>
        </header>

        <section className="mt-6 grid gap-4 lg:grid-cols-3">
          {(Object.keys(ROLE_META) as ModelRole[]).map((role) => {
            const selection = profile[role];
            const selectedProvider = providerMap.get(selection.provider);

            return (
              <article
                key={role}
                className="rounded-xl border border-[var(--border-color)] bg-[var(--card-bg)] p-5"
              >
                <h2 className="font-semibold">{ROLE_META[role].title}</h2>
                <p className="mt-2 min-h-12 text-xs leading-5 text-[var(--muted)]">
                  {ROLE_META[role].description}
                </p>

                <label className="mt-5 block text-xs font-medium text-[var(--muted)]">
                  Provider
                </label>
                <select
                  value={selection.provider}
                  onChange={(event) => updateRole(role, 'provider', event.target.value)}
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2 text-sm"
                >
                  {config.providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.name}
                    </option>
                  ))}
                </select>

                <label className="mt-4 block text-xs font-medium text-[var(--muted)]">
                  模型
                </label>
                <select
                  value={selection.model}
                  onChange={(event) => updateRole(role, 'model', event.target.value)}
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2 text-sm"
                >
                  {selectedProvider?.models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name}
                    </option>
                  ))}
                </select>
              </article>
            );
          })}
        </section>

        <section className="mt-6 grid gap-6 rounded-xl border border-[var(--border-color)] p-6 lg:grid-cols-[1.2fr_0.8fr]">
          <div>
            <h2 className="text-lg font-semibold">向量检索</h2>
            <p className="mt-1 text-sm text-[var(--muted)]">
              Embedding 变化后必须重建索引；正式版会自动识别并提示迁移。
            </p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <label className="text-sm">
                <span className="block text-xs font-medium text-[var(--muted)]">Provider</span>
                <select
                  value={profile.embeddingProvider}
                  onChange={(event) => {
                    const provider = event.target.value as ModelProfile['embeddingProvider'];
                    setProfile({
                      ...profile,
                      embeddingProvider: provider,
                      embeddingModel: EMBEDDING_DEFAULTS[provider],
                    });
                  }}
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2"
                >
                  <option value="openai">OpenAI</option>
                  <option value="google">Google</option>
                  <option value="ollama">Ollama（本地）</option>
                  <option value="bedrock">AWS Bedrock</option>
                </select>
              </label>
              <label className="text-sm">
                <span className="block text-xs font-medium text-[var(--muted)]">模型</span>
                <input
                  value={profile.embeddingModel}
                  onChange={(event) =>
                    setProfile({ ...profile, embeddingModel: event.target.value })
                  }
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2"
                />
              </label>
            </div>
          </div>

          <div className="border-t border-[var(--border-color)] pt-5 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
            <h2 className="text-lg font-semibold">夜间分析限制</h2>
            <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
              <label className="text-sm">
                <span className="block text-xs font-medium text-[var(--muted)]">最大并发任务</span>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={profile.maxConcurrency}
                  onChange={(event) =>
                    setProfile({ ...profile, maxConcurrency: Number(event.target.value) })
                  }
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2"
                />
              </label>
              <label className="text-sm">
                <span className="block text-xs font-medium text-[var(--muted)]">
                  单晚预算（0 表示不限制）
                </span>
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={profile.nightlyBudget}
                  onChange={(event) =>
                    setProfile({ ...profile, nightlyBudget: Number(event.target.value) })
                  }
                  className="mt-2 w-full rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-3 py-2"
                />
              </label>
            </div>
          </div>
        </section>

        <section className="mt-6 flex flex-col gap-4 rounded-xl border border-[var(--border-color)] bg-[var(--card-bg)] p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="font-semibold">安全说明</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
              当前页面只保存模型名称和策略，不保存 API Key。桌面版使用系统 Keychain，VS Code 版使用 SecretStorage。
            </p>
            {status && <p className="mt-2 text-sm text-[var(--accent-primary)]">{status}</p>}
          </div>
          <div className="flex shrink-0 gap-3">
            <button
              type="button"
              onClick={checkService}
              className="rounded-lg border border-[var(--border-color)] px-4 py-2 text-sm hover:bg-[var(--background)]"
            >
              检查本地服务
            </button>
            <button
              type="button"
              onClick={save}
              className="rounded-lg bg-[var(--accent-primary)] px-4 py-2 text-sm font-medium text-white"
            >
              保存配置
            </button>
          </div>
        </section>
      </div>
    </main>
  );
}
