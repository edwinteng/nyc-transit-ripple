"""GTFS-Realtime protobuf classes with the NYCT subway extension.

The standard GTFS-Realtime schema comes from ``gtfs-realtime-bindings``. The MTA
adds an extension (field 1001) carrying the train id, whether a train has been
assigned to the trip, and scheduled/actual tracks. We register both schemas in a
private descriptor pool so the extension is decoded without colliding with any
other library that might register the same names in the default pool.
"""

from __future__ import annotations

from functools import lru_cache
from types import SimpleNamespace

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.transit import gtfs_realtime_pb2

NYCT_PROTO_NAME = "transit_ripple/nyct-subway.proto"
_EXT_NUMBER = 1001


def _nyct_file_proto(base_name: str) -> descriptor_pb2.FileDescriptorProto:
    f = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(
        name=NYCT_PROTO_NAME,
        package="transit_realtime",
        syntax="proto2",
        dependency=[base_name],
    )

    trip = fdp.message_type.add(name="NyctTripDescriptor")
    trip.field.add(name="train_id", number=1, type=f.TYPE_STRING, label=f.LABEL_OPTIONAL)
    trip.field.add(name="is_assigned", number=2, type=f.TYPE_BOOL, label=f.LABEL_OPTIONAL)
    direction = trip.enum_type.add(name="Direction")
    for name, number in (("NORTH", 1), ("EAST", 2), ("SOUTH", 3), ("WEST", 4)):
        direction.value.add(name=name, number=number)
    trip.field.add(
        name="direction",
        number=3,
        type=f.TYPE_ENUM,
        type_name=".transit_realtime.NyctTripDescriptor.Direction",
        label=f.LABEL_OPTIONAL,
    )

    stu = fdp.message_type.add(name="NyctStopTimeUpdate")
    stu.field.add(name="scheduled_track", number=1, type=f.TYPE_STRING, label=f.LABEL_OPTIONAL)
    stu.field.add(name="actual_track", number=2, type=f.TYPE_STRING, label=f.LABEL_OPTIONAL)

    fdp.extension.add(
        name="nyct_trip_descriptor",
        number=_EXT_NUMBER,
        type=f.TYPE_MESSAGE,
        type_name=".transit_realtime.NyctTripDescriptor",
        extendee=".transit_realtime.TripDescriptor",
        label=f.LABEL_OPTIONAL,
    )
    fdp.extension.add(
        name="nyct_stop_time_update",
        number=_EXT_NUMBER,
        type=f.TYPE_MESSAGE,
        type_name=".transit_realtime.NyctStopTimeUpdate",
        extendee=".transit_realtime.TripUpdate.StopTimeUpdate",
        label=f.LABEL_OPTIONAL,
    )
    return fdp


@lru_cache(maxsize=1)
def schema() -> SimpleNamespace:
    """Return message classes and extension handles (built once per process)."""
    base = descriptor_pb2.FileDescriptorProto()
    gtfs_realtime_pb2.DESCRIPTOR.CopyToProto(base)

    pool = descriptor_pool.DescriptorPool()
    pool.Add(base)
    pool.Add(_nyct_file_proto(base.name))
    classes = message_factory.GetMessageClassesForFiles([base.name, NYCT_PROTO_NAME], pool)

    return SimpleNamespace(
        pool=pool,
        FeedMessage=classes["transit_realtime.FeedMessage"],
        NyctTripDescriptor=classes["transit_realtime.NyctTripDescriptor"],
        NyctStopTimeUpdate=classes["transit_realtime.NyctStopTimeUpdate"],
        nyct_trip=pool.FindExtensionByName("transit_realtime.nyct_trip_descriptor"),
        nyct_stop=pool.FindExtensionByName("transit_realtime.nyct_stop_time_update"),
    )
