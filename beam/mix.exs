defmodule Beam.MixProject do
  use Mix.Project

  # Umbrella root for the Vitrial BEAM services.
  #
  # Shared _build/deps/mix.lock live at the repository root (one level up) so a
  # single dependency set and a single lockfile govern every service app. This
  # is deliberate: the project doctrine is minimal dependency surface, and one
  # resolved set is easier to audit for CVEs than eight independent ones.

  def project do
    [
      app: :beam,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      apps_path: "apps",
      build_path: "../_build",
      deps_path: "../deps",
      lockfile: "../mix.lock",
      elixirc_paths: ["lib"],
      test_elixirc_paths: ["test"],
      dialyzer: [plt_add_apps: [:mix]]
    ]
  end

  def application do
    [
      extra_applications: [:logger, :crypto, :public_key, :ssl]
    ]
  end

  defp deps do
    []
  end
end
