defmodule VitrialWebTest do
  use ExUnit.Case
  doctest VitrialWeb

  test "greets the world" do
    assert VitrialWeb.hello() == :world
  end
end
