import { useState } from "react";

import { useAppSettings } from "@/contexts/AppSettingsContext";
import { Badge } from "@/ds/Badge/Badge";
import { Button as DsButton } from "@/ds/Button/Button";
import { Text } from "@/ds/Text/Text";
import { useMcpStatus } from "@/hooks/use-mcp-status";

import {
  SettingsCallout,
  SettingsHint,
  SettingsNestedGroup,
  SettingsPanel,
  SettingsReadonlyField,
  SettingsStack,
  SettingsSubsection,
} from "./SettingsContent";
import { SettingToggle } from "./SettingToggle";

type ConfigTab = "stdio" | "http";

export function McpSettingsSection() {
  const { settings, updateSettings } = useAppSettings();
  const { status, clientConfig, rotating, error, rotateToken } = useMcpStatus(settings.mcpEnabled);
  const [revealToken, setRevealToken] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [configTab, setConfigTab] = useState<ConfigTab>("stdio");

  const copyText = (key: string, value: string) => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopiedKey(key);
      window.setTimeout(() => {
        setCopiedKey((curr) => (curr === key ? null : curr));
      }, 1500);
    });
  };

  const maskedToken = clientConfig?.token
    ? `${clientConfig.token.slice(0, 9)}...${clientConfig.token.slice(-4)}`
    : "Not generated";

  const configSnippet = clientConfig
    ? JSON.stringify(
        configTab === "stdio" ? clientConfig.stdioConfig : clientConfig.streamableHttpConfig,
        null,
        2,
      )
    : "";

  return (
    <SettingsSubsection
      showDivider
      title="MCP Server"
      description="Expose LTX Desktop tools, resources, and prompts over loopback-only Model Context Protocol (127.0.0.1) for AI agents."
      badge={
        settings.mcpEnabled ? (
          <Badge
            appearance={status?.serving ? "success" : "neutral"}
            size="sm"
            text={status?.serving ? "Listening" : "Starting"}
          />
        ) : undefined
      }
    >
      <SettingsStack loose>
        <SettingToggle
          rowLayout="preference"
          title="Enable MCP Server"
          description="Starts an authenticated Streamable HTTP endpoint on localhost:41956/mcp."
          enabled={settings.mcpEnabled}
          onToggle={() => updateSettings({ mcpEnabled: !settings.mcpEnabled })}
        />

        {settings.mcpEnabled ? (
          <SettingsNestedGroup>
            <SettingsStack>
              <SettingToggle
                rowLayout="preference"
                nested
                title="Allow local media import"
                description="Lets MCP clients import image, video, or audio files from disk into your asset library."
                enabled={settings.mcpAllowLocalImport}
                onToggle={() =>
                  updateSettings({ mcpAllowLocalImport: !settings.mcpAllowLocalImport })
                }
              />

              <SettingsPanel inset>
                <SettingsStack>
                  <div className="flex items-center justify-between gap-3">
                    <Text as="span" variant="label" size="xs">
                      Endpoint URL
                    </Text>
                    <SettingsReadonlyField
                      value={status?.url ?? clientConfig?.url ?? "http://127.0.0.1:41956/mcp"}
                    />
                  </div>

                  <div className="flex items-center justify-between gap-2">
                    <Text as="span" variant="label" size="xs">
                      Bearer token
                    </Text>
                    <div className="flex items-center gap-2">
                      <SettingsReadonlyField
                        value={
                          revealToken && clientConfig?.token ? clientConfig.token : maskedToken
                        }
                      />
                      <DsButton
                        appearance="neutral"
                        hierarchy="secondary"
                        size="sm"
                        label={revealToken ? "Hide" : "Reveal"}
                        disabled={!clientConfig?.token}
                        onClick={() => setRevealToken((curr) => !curr)}
                      />
                      <DsButton
                        appearance="neutral"
                        hierarchy="secondary"
                        size="sm"
                        label={copiedKey === "token" ? "Copied" : "Copy"}
                        disabled={!clientConfig?.token}
                        onClick={() => {
                          if (clientConfig?.token) copyText("token", clientConfig.token);
                        }}
                      />
                      <DsButton
                        appearance="neutral"
                        hierarchy="secondary"
                        size="sm"
                        label="Rotate"
                        isLoading={rotating}
                        disabled={rotating}
                        onClick={() => void rotateToken()}
                      />
                    </div>
                  </div>

                  {configSnippet ? (
                    <SettingsCallout>
                      <div className="flex items-center justify-between gap-2 pb-2">
                        <div className="flex items-center gap-2">
                          <DsButton
                            appearance={configTab === "stdio" ? "brand" : "neutral"}
                            hierarchy={configTab === "stdio" ? "primary" : "secondary"}
                            size="sm"
                            label="Stdio Bridge (Recommended)"
                            onClick={() => setConfigTab("stdio")}
                          />
                          <DsButton
                            appearance={configTab === "http" ? "brand" : "neutral"}
                            hierarchy={configTab === "http" ? "primary" : "secondary"}
                            size="sm"
                            label="Streamable HTTP"
                            onClick={() => setConfigTab("http")}
                          />
                        </div>
                        <DsButton
                          appearance="neutral"
                          hierarchy="secondary"
                          size="sm"
                          label={copiedKey === "config" ? "Copied" : "Copy config"}
                          onClick={() => copyText("config", configSnippet)}
                        />
                      </div>
                      <pre className="overflow-x-auto text-xs leading-relaxed">
                        <code>{configSnippet}</code>
                      </pre>
                    </SettingsCallout>
                  ) : null}
                </SettingsStack>
              </SettingsPanel>

              {status && !status.serving && status.reason ? (
                <SettingsHint variant="warning">{status.reason}</SettingsHint>
              ) : null}
              {error ? <SettingsHint variant="warning">{error}</SettingsHint> : null}
            </SettingsStack>
          </SettingsNestedGroup>
        ) : null}
      </SettingsStack>
    </SettingsSubsection>
  );
}
